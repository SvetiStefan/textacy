# -*- coding: utf-8 -*-
"""
Wikipedia
---------

All articles for a given language- and version-specific Wikipedia site snapshot,
as either texts (str) or records (dict) with both text and metadata.

Records include the following fields:

    * ``text``: text content of the article, with markup stripped out
    * ``title``: title of the Wikipedia article
    * ``page_id``: unique identifier of the page, usable in Wikimedia APIs
    * ``wiki_links``: a list of other article pages linked to from this page
    * ``ext_links``: a list of external URLs linked to from this page
    * ``categories``: a list of Wikipedia categories to which this page belongs
"""
from __future__ import unicode_literals

import logging
import os
import re
from xml.etree.cElementTree import iterparse

import ftfy

from textacy import data_dir
from textacy import compat
from textacy.datasets.base import Dataset
from textacy import fileio

LOGGER = logging.getLogger(__name__)

NAME = 'wikipedia'
DESCRIPTION = ('All articles for a given language- and version-specific '
               'Wikipedia site snapshot.')
SITE_URL = 'https://meta.wikimedia.org/wiki/Data_dumps'
DATA_DIR = os.path.join(data_dir, NAME)

DOWNLOAD_ROOT = 'https://dumps.wikimedia.org/'

# nowiki tags: take contents verbatim
re_nowiki = re.compile(r'<nowiki>(.*?)</nowiki>', flags=re.UNICODE)

self_closing_tags = ('br', 'hr', 'nobr', 'ref', 'references')
re_self_closing_html_tags = re.compile(
    r'<\s?(%s)\b[^>]*/\s?>' % '|'.join(self_closing_tags),
    flags=re.IGNORECASE | re.DOTALL)

ignored_html_tags = (
    'abbr', 'b', 'big', 'blockquote', 'center', 'cite', 'em', 'font', 'h1', 'h2',
    'h3', 'h4', 'hiero', 'i', 'kbd', 'p', 'plaintext', 's', 'span', 'strike',
    'strong', 'tt', 'u', 'var', 'math', 'code')  # are math and code okay here?
re_ignored_html_tags = re.compile(
    r'<(%s)\b.*?>(.*?)</\s*\1>' % '|'.join(ignored_html_tags),
    flags=re.IGNORECASE | re.DOTALL)

dropped_elements = (
    'caption', 'dd', 'dir', 'div', 'dl', 'dt', 'form', 'gallery', 'imagemap', 'img',
    'indicator', 'input', 'li', 'menu', 'noinclude', 'ol', 'option', 'pre', 'ref',
    'references', 'select', 'small', 'source', 'sub', 'sup', 'table', 'td',
    'textarea', 'th', 'timeline', 'tr', 'ul')
re_dropped_elements = re.compile(
    r'<\s*(%s)\b[^>/]*>.*?<\s*/\s*\1>' % '|'.join(dropped_elements),
    flags=re.IGNORECASE | re.DOTALL)

# text formatting
re_italic_quote = re.compile(r"''\"([^\"]*?)\"''")
re_bold_italic = re.compile(r"'{2,5}(.*?)'{2,5}")
re_quote_quote = re.compile(r'""([^"]*?)""')

# text normalization
re_spaces = re.compile(r' {2,}')
re_linebreaks = re.compile(r'\n{3,}')
re_dots = re.compile(r'\.{4,}')
re_brackets = re.compile(r'\[\s?\]|\(\s?\)')

re_comments = re.compile('<!--.*?-->', flags=re.UNICODE | re.DOTALL)
re_categories = re.compile(r'\[\[Category:[^\]\[]*\]\]', flags=re.UNICODE)
re_link_trails = re.compile(r'\w+', flags=re.UNICODE)
re_ext_link = re.compile(r'(?<!\[)\[([^\[\]]*?)\]')
re_table_formatting = re.compile('\n\s*(({\|)|(\|-+)|(\|})).*?(?=\n)', flags=re.UNICODE)
re_table_cell_formatting = re.compile('\n\s*(\||\!)(.*?\|)*([^|]*?)', flags=re.UNICODE)
re_headings = re.compile(r'(={2,6})\s*(.*?)\s*\1')
re_files_images = re.compile(
    '\n\[\[(?:Image|File)(?:.*?)(\|.*?)*\|(.*?)\]\]',
    flags=re.IGNORECASE | re.UNICODE)
re_random_cruft = re.compile(' (,:\.\)\]»)|(\[\(«) ', flags=re.UNICODE)

magic_words = (
    '__NOTOC__', '__FORCETOC__', '__TOC__', '__NEWSECTIONLINK__', '__NONEWSECTIONLINK__',
    '__NOGALLERY__', '__HIDDENCAT__', '__NOCONTENTCONVERT__', '__NOCC__', '__NOTITLECONVERT__',
    '__NOTC__', '__START__', '__END__', '__INDEX__', '__NOINDEX__',
    '__STATICREDIRECT__', '__DISAMBIG__')
re_magic_words = re.compile('|'.join(magic_words))


class Wikipedia(Dataset):
    """
    Stream Wikipedia articles from versioned, language-specific database dumps,
    either as texts (str) or records (dict) with both text content and metadata.

    Download a database dump for a given language and version::

        >>> wp = Wikipedia(lang='en', version='latest')
        >>> wp.download()
        >>> wp.info
        {'data_dir': 'path/to/textacy/data/wikipedia',
         'description': 'All articles for a given Wikimedia wiki, including wikitext source and metadata, as a single database dump in XML format.',
         'name': 'wikipedia',
         'site_url': 'https://meta.wikimedia.org/wiki/Data_dumps'}

    Iterate over articles as plain texts or records with both text and metadata::

        >>> for text in wp.texts(limit=5):
        ...     print(text)
        >>> for record in wp.records(limit=5):
        ...     print(record['title'], record['text'][:500])

    Filter articles by text length::

        >>> for text in wp.texts(min_len=1000, limit=1):
        ...     print(text)

    Args:
        data_dir (str): Path to directory on disk under which database dump
            files are stored. Each file is expected at
            ``{lang}wiki/{version}/{lang}wiki-{version}-pages-articles.xml.bz2``
            immediately under this directory.
        lang (str): Standard two-letter language code, e.g. "en" => "English",
            "de" => "German". https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes
        version (str): Database dump version to use. Either "latest" for the
            most recently available version or a date formatted as "YYYYMMDD".
            Dumps are produced intermittently; check for available versions at
            https://meta.wikimedia.org/wiki/Data_dumps.

    Attributes:
        lang (str): Standard two-letter language code used in instantiation.
        version (str): Database dump version used in instantiation.
        filestub (str): The component of ``filename`` that is unique to this
            lang- and version-specific database dump.
        filename (str): Full path on disk for the lang- and version-specific
            Wikipedia database dump, found under the ``data_dir`` directory.
    """

    def __init__(self, data_dir=DATA_DIR, lang='en', version='latest'):
        super(Wikipedia, self).__init__(
            name=NAME, description=DESCRIPTION, site_url=SITE_URL, data_dir=data_dir)
        self.lang = lang
        self.version = version
        self.filestub = '{lang}wiki/{version}/{lang}wiki-{version}-pages-articles.xml.bz2'.format(
            version=self.version, lang=self.lang)
        self._filename = os.path.join(data_dir, self.filestub)

    @property
    def filename(self):
        """
        str: Full path on disk for Wikipedia database dump corresponding to
            the ``lang`` and ``version`` used in instantiation.
            ``None`` if file not found.
        """
        if os.path.isfile(self._filename):
            return self._filename
        else:
            return None

    def download(self, force=False):
        """
        Download the Wikipedia database dump corresponding to the ``lang`` and
        ``version`` used in instantiation, and save it to disk under the
        ``data_dir`` directory.

        Args:
            force (bool): Download the file, even if it already exists on disk.
        """
        url = compat.urljoin(DOWNLOAD_ROOT, self.filestub)
        fname = self._filename
        if os.path.isfile(fname) and force is False:
            LOGGER.warning(
                'File %s already exists; skipping download...', fname)
            return
        LOGGER.info(
            'Downloading data from %s and writing it to %s', url, fname)
        fileio.write_streaming_download_file(
            url, fname, mode='wb', encoding=None,
            auto_make_dirs=True, chunk_size=1024)

    def __iter__(self):
        """
        Iterate over the pages of a Wikipedia articles database dump (*articles.xml.bz2),
        yielding one (page id, page title, page content) 3-tuple at a time.

        Yields:
            Tuple[str, str, str]: page id, title, content with wikimedia markup
        """
        if not self.filename:
            raise IOError('{} file not found'.format(self._filename))

        if compat.is_python2 is False:
            events = ('end',)
            f = fileio.open_sesame(self.filename, mode='rt', encoding="UTF-8")
        else:  # Python 2 can't open bzip in text mode :(
            events = (b'end',)
            f = fileio.open_sesame(self.filename, mode='rb')
        with f:

            elems = (elem for _, elem in iterparse(f, events=events))

            elem = next(elems)
            match = re.match('^{(.*?)}', elem.tag)
            namespace = match.group(1) if match else ''
            if not namespace.startswith('http://www.mediawiki.org/xml/export-'):
                raise ValueError(
                    'namespace "{}" not a valid MediaWiki dump namespace'.format(namespace))

            page_tag = '{%s}page' % namespace
            ns_path = './{%s}ns' % namespace
            page_id_path = './{%s}id' % namespace
            title_path = './{%s}title' % namespace
            text_path = './{%s}revision/{%s}text' % (namespace, namespace)

            for elem in elems:
                if elem.tag == page_tag:
                    page_id = elem.find(page_id_path).text
                    title = elem.find(title_path).text
                    ns = elem.find(ns_path).text
                    if ns != '0':
                        content = ''
                    else:
                        content = elem.find(text_path).text
                    if content is None:
                        content = ''
                    elif not isinstance(content, compat.unicode_):
                        content = compat.bytes_to_unicode(content, errors='ignore')
                    yield page_id, title, content
                    elem.clear()

    def _parse_content(self, content, parser, fast):
        unicode_ = compat.unicode_  # for performance
        wikicode = parser.parse(content)

        parsed_content = {}

        wikilinks = [unicode_(wc.title) for wc in wikicode.ifilter_wikilinks()]
        parsed_content['categories'] = [
            wc for wc in wikilinks
            if wc.startswith('Category:')]
        parsed_content['wiki_links'] = [
            wc for wc in wikilinks
            if not wc.startswith('Category:') and
            not wc.startswith('File:') and
            not wc.startswith('Image:')]
        parsed_content['ext_links'] = [
            unicode_(wc.url) for wc in wikicode.ifilter_external_links()]

        # worse quality, but significantly faster
        # just strip the markup from unicode, as is done for `.texts()`
        if fast is True:
            parsed_content['text'] = strip_markup(unicode_(wikicode))
        else:
            re_image_wl = re.compile(
                '^(?:File|Image|Media):', flags=re.IGNORECASE | re.UNICODE)
            bad_template_names = {
                'reflist', 'notelist', 'notelist-ua', 'notelist-lr', 'notelist-ur', 'notelist-lg'}
            bad_tags = {'ref', 'table'}

            def is_bad_wikilink(obj):
                return bool(re_image_wl.match(unicode_(obj.title)))

            def is_bad_tag(obj):
                return unicode_(obj.tag) in bad_tags

            def is_bad_template(obj):
                return obj.name.lower() in bad_template_names

            texts = []
            # strip out references, tables, and file/image links
            # then concatenate the stripped text of each section
            for i, section in enumerate(wikicode.get_sections(flat=True, include_lead=True, include_headings=True)):
                for obj in section.ifilter_wikilinks(matches=is_bad_wikilink, recursive=True):
                    try:
                        section.remove(obj)
                    except Exception:
                        continue
                for obj in section.ifilter_templates(matches=is_bad_template, recursive=True):
                    try:
                        section.remove(obj)
                    except Exception:
                        continue
                for obj in section.ifilter_tags(matches=is_bad_tag, recursive=True):
                    try:
                        section.remove(obj)
                    except Exception:
                        continue
                texts.append(section.strip_code().strip())

            parsed_content['text'] = '\n\n'.join(texts)

        return parsed_content

    def texts(self, min_len=100, limit=-1):
        """
        Iterate over the pages in a Wikipedia articles database dump
        (``*articles.xml.bz2``), yielding the text of a page, one at a time.

        Args:
            min_len (int): minimum length in chars that a page must have
                for it to be returned; too-short pages are skipped (optional)
            limit (int): maximum number of pages (passing `min_len`) to yield;
                if -1, all pages in the db dump are iterated over (optional)

        Yields:
            str: plain text for the next page in the wikipedia database dump

        Notes:
            Page and section titles appear immediately before the text content
                that they label, separated by an empty line.
        """
        n_pages = 0
        for _, title, content in self:
            text = strip_markup(content)
            if len(text) < min_len:
                continue

            yield title + '\n\n' + text

            n_pages += 1
            if n_pages == limit:
                break

    def records(self, min_len=100, limit=-1, fast=False):
        """
        Iterate over the pages in a Wikipedia articles database dump
        (``*articles.xml.bz2``), yielding one page whose structure and content
        have been parsed, as a dict.

        Args:
            min_len (int): minimum length in chars that a page must have
                for it to be returned; too-short pages are skipped
            limit (int): maximum number of pages (passing ``min_len``) to yield;
                if -1, all pages in the db dump are iterated over (optional)
            fast (bool): If True, text is extracted using a faster method but
                which gives lower quality results. Otherwise, a slower but better
                method is used to extract article text.

        Yields:
            dict: the next page's parsed content, including key:value pairs for
                'title', 'page_id', 'text', 'categories', 'wiki_links', 'ext_links'

        Notes:
            This function requires `mwparserfromhell <mwparserfromhell.readthedocs.org>`_
        """
        # hiding this here; don't want another required dep
        try:
            import mwparserfromhell
        except ImportError:
            LOGGER.exception(
                'mwparserfromhell package must be installed; '
                'see http://pythonhosted.org/mwparserfromhell/')
            raise
        parser = mwparserfromhell.parser.Parser()

        n_pages = 0
        for page_id, title, content in self:

            page = self._parse_content(content, parser, fast)
            if len(page['text']) < min_len:
                continue
            page['title'] = title
            page['page_id'] = page_id
            page['text'] = title + '\n\n' + page['text']

            yield page

            n_pages += 1
            if n_pages == limit:
                break


def strip_markup(wikitext):
    """
    Strip Wikimedia markup from the text content of a Wikipedia page and return
    the page as plain-text.

    Args:
        wikitext (str)

    Returns:
        str
    """
    if not wikitext:
        return ''

    # remove templates
    text = remove_templates(wikitext)

    # remove irrelevant spans
    text = re_comments.sub('', text)
    text = re_ignored_html_tags.sub(r'\2', text)
    text = re_self_closing_html_tags.sub('', text)
    text = re_dropped_elements.sub('', text)
    text = re_categories.sub('', text)
    text = re_files_images.sub('', text)  # TODO: keep file/image captions?

    # replace external links with just labels or just URLs
    text = replace_external_links(text)

    # drop magic words behavioral switches
    text = re_magic_words.sub('', text)

    # replace internal links with just their labels
    text = replace_internal_links(text)
    # text = replace_internal_links(text)  # TODO: is this needed?

    # remove table markup
    text = text.replace('||', '\n|').replace('!!', '\n!')  # put each cell on a separate line
    text = re_table_formatting.sub('\n', text)  # remove formatting lines
    text = re_table_cell_formatting.sub('\n\\3', text)  # leave only cell content

    # strip out text formatting
    text = re_italic_quote.sub(r'"\1"', text)
    text = re_bold_italic.sub(r'\1', text)
    text = re_quote_quote.sub(r'"\1"', text)

    # unescape html entities
    text = ftfy.fixes.unescape_html(text)

    # final cleanup
    text = re_headings.sub(r'\n\n\2\n\n', text)
    text = re_dots.sub('...', text)
    text = re_brackets.sub(r'', text)
    text = text.replace('[[', '').replace(']]', '')
    text = text.replace('<<', '«').replace('>>', '»')
    text = re_random_cruft.sub(r'\1', text)
    text = re.sub(r'\n\W+?\n', r'\n', text, flags=re.UNICODE)
    text = text.replace(',,', ',').replace(',.', '.')
    text = re_spaces.sub(' ', text)
    text = re_linebreaks.sub(r'\n\n', text)

    return text.strip()


def remove_templates(wikitext):
    """
    Return ``wikitext`` with all wikimedia markup templates removed,
    where templates are identified by opening '{{' and closing '}}'.

    See also:
        http://meta.wikimedia.org/wiki/Help:Template
    """
    pieces = []
    cur_idx = 0
    for s, e in get_delimited_spans(wikitext, open_delim='{{', close_delim='}}'):
        pieces.append(wikitext[cur_idx: s])
        cur_idx = e
    return ''.join(pieces)
    # below is gensim's solution; it's slow...
    # n_openings = 0
    # n_closings = 0
    # opening_idxs = []
    # closing_idxs = []
    # in_template = False
    # prev_char = None
    # for i, char in enumerate(wikitext):
    #     if not in_template:
    #         if char == '{' and prev_char == '{':
    #             opening_idxs.append(i - 1)
    #             in_template = True
    #             n_openings = 2
    #     else:
    #         if char == '{':
    #             n_openings += 1
    #         elif char == '}':
    #             n_closings += 1
    #         if n_openings == n_closings:
    #             closing_idxs.append(i)
    #             in_template = False
    #             n_openings = 0
    #             n_closings = 0
    #     prev_char = char
    # return ''.join(
    #     wikitext[closing_idx + 1: opening_idx]
    #     for opening_idx, closing_idx in zip(opening_idxs + [None], [-1] + closing_idxs))


def get_delimited_spans(wikitext, open_delim='[[', close_delim=']]'):
    """
    Args:
        wikitext (str)
        open_delim (str)
        close_delim (str)

    Yields:
        Tuple[int, int]: start and end index of next span delimited by
            ``open_delim`` on the left and ``close_delim`` on the right
    """
    open_pattern = re.escape(open_delim)
    re_open = re.compile(open_pattern, flags=re.UNICODE)
    re_open_or_close = re.compile(
        open_pattern + '|' + re.escape(close_delim), flags=re.UNICODE)

    openings = []
    cur_idx = 0
    started = False
    re_next = re_open

    while True:
        next_span = re_next.search(wikitext, pos=cur_idx)
        if next_span is None:
            return

        if started is False:
            start = next_span.start()
            started = True

        delim = next_span.group(0)
        if delim == open_delim:
            openings.append(delim)
            re_next = re_open_or_close
        else:
            openings.pop()
            if openings:
                re_next = re_open_or_close
            else:
                yield (start, next_span.end())
                re_next = re_open
                start = next_span.end()
                started = False

        cur_idx = next_span.end()


def replace_internal_links(wikitext):
    """
    Replace internal links of the form ``[[title |...|label]]trail``
    with just ``label``.
    """
    pieces = []
    cur_idx = 0
    for s, e in get_delimited_spans(wikitext, '[[', ']]'):
        link_trail = re_link_trails.match(wikitext, pos=e)
        if link_trail is not None:
            end = link_trail.end()
            link_trail = link_trail.group()
        else:
            end = e
            link_trail = ''
        span_content = wikitext[s + 2: e - 2]
        pipe_idx = span_content.find('|')
        if pipe_idx < 0:
            label = span_content
        else:
            last_pipe_idx = span_content[pipe_idx - 1:].rfind('|')
            label = span_content[pipe_idx + last_pipe_idx:].strip()
        pieces.append(wikitext[cur_idx: s])
        pieces.append(label)
        cur_idx = end
    # add leftovers
    pieces.append(wikitext[cur_idx:])

    return ''.join(pieces)


def replace_external_links(wikitext):
    """
    Replace external links of the form ``[URL text]`` with just
    ``text`` if present or just ``URL`` if not.

    See also:
        https://www.mediawiki.org/wiki/Help:Links#External_links
    """
    pieces = []
    cur_idx = 0
    for match in re_ext_link.finditer(wikitext):
        content = match.group(1)
        space_idx = content.find(' ')
        label = content[space_idx + 1:] if space_idx > 0 else content
        pieces.append(wikitext[cur_idx: match.start()])
        pieces.append(label)
        cur_idx = match.end()
    # add leftovers
    pieces.append(wikitext[cur_idx:])

    return ''.join(pieces)
