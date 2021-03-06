"""
CLI argument parsing logic.

"""
import os
import sys
import re
import json
import argparse
import mimetypes
import getpass

try:
    from collections import OrderedDict
except ImportError:
    OrderedDict = dict

from requests.structures import CaseInsensitiveDict
from requests.compat import str

from . import __version__


SEP_COMMON = ':'
SEP_HEADERS = SEP_COMMON
SEP_DATA = '='
SEP_DATA_RAW_JSON = ':='
SEP_FILES = '@'
SEP_QUERY = '=='
DATA_ITEM_SEPARATORS = [
    SEP_DATA,
    SEP_DATA_RAW_JSON,
    SEP_FILES
]


OUT_REQ_HEAD = 'H'
OUT_REQ_BODY = 'B'
OUT_RESP_HEAD = 'h'
OUT_RESP_BODY = 'b'
OUTPUT_OPTIONS = [OUT_REQ_HEAD,
                  OUT_REQ_BODY,
                  OUT_RESP_HEAD,
                  OUT_RESP_BODY]


PRETTIFY_STDOUT_TTY_ONLY = object()

DEFAULT_UA = 'HTTPie/%s' % __version__


class Parser(argparse.ArgumentParser):

    def __init__(self, *args, **kwargs):
        kwargs['add_help'] = False
        super(Parser, self).__init__(*args, **kwargs)
        # Help only as --help (-h is used for --headers).
        self.add_argument('--help',
            action='help', default=argparse.SUPPRESS,
            help=argparse._('show this help message and exit'))

    def parse_args(self, env, args=None, namespace=None):

        args = super(Parser, self).parse_args(args, namespace)

        self._process_output_options(args, env)
        self._guess_method(args, env)
        self._parse_items(args)

        if not env.stdin_isatty:
            self._body_from_file(args, env.stdin)
        if args.auth and not args.auth.has_password():
            # stdin has already been read (if not a tty) so
            # it's save to prompt now.
            args.auth.prompt_password()

        return args

    def _body_from_file(self, args, f):
        if args.data:
            self.error('Request body (from stdin or a file) and request '
                       'data (key=value) cannot be mixed.')
        args.data = f.read()

    def _guess_method(self, args, env):
        """
        Set `args.method`, if not specified, to either POST or GET
        based on whether the request has data or not.

        """
        if args.method is None:
            # Invoked as `http URL'.
            assert not args.items
            if not env.stdin_isatty:
                args.method = 'POST'
            else:
                args.method = 'GET'
        # FIXME: False positive, e.g., "localhost" matches but is a valid URL.
        elif not re.match('^[a-zA-Z]+$', args.method):
            # Invoked as `http URL item+':
            # - The URL is now in `args.method`.
            # - The first item is now in `args.url`.
            #
            # So we need to:
            # - Guess the HTTP method.
            # - Set `args.url` correctly.
            # - Parse the first item and move it to `args.items[0]`.

            item = KeyValueArgType(
                SEP_COMMON,
                SEP_QUERY,
                SEP_DATA,
                SEP_DATA_RAW_JSON,
                SEP_FILES).__call__(args.url)

            args.url = args.method
            args.items.insert(0, item)

            has_data = not env.stdin_isatty or any(
                item.sep in DATA_ITEM_SEPARATORS for item in args.items)
            if has_data:
                args.method = 'POST'
            else:
                args.method = 'GET'

    def _parse_items(self, args):
        """
        Parse `args.items` into `args.headers`,
        `args.data`, `args.`, and `args.files`.

        """
        args.headers = CaseInsensitiveDict()
        args.headers['User-Agent'] = DEFAULT_UA
        args.data = ParamDict() if args.form else OrderedDict()
        args.files = OrderedDict()
        args.params = ParamDict()
        try:
            parse_items(items=args.items,
                        headers=args.headers,
                        data=args.data,
                        files=args.files,
                        params=args.params)
        except ParseError as e:
            if args.traceback:
                raise
            self.error(e.message)

        if args.files and not args.form:
            # `http url @/path/to/file`
            # It's not --form so the file contents will be used as the
            # body of the requests. Also, we try to detect the appropriate
            # Content-Type.
            if len(args.files) > 1:
                self.error(
                    'Only one file can be specified unless'
                    ' --form is used. File fields: %s'
                    % ','.join(args.files.keys()))
            f = list(args.files.values())[0]
            self._body_from_file(args, f)
            args.files = {}
            if 'Content-Type' not in args.headers:
                mime, encoding = mimetypes.guess_type(f.name, strict=False)
                if mime:
                    content_type = mime
                    if encoding:
                        content_type = '%s; charset=%s' % (mime, encoding)
                    args.headers['Content-Type'] = content_type

    def _process_output_options(self, args, env):
        if not args.output_options:
            if env.stdout_isatty:
                args.output_options = OUT_RESP_HEAD + OUT_RESP_BODY
            else:
                args.output_options = OUT_RESP_BODY

        unknown = set(args.output_options) - set(OUTPUT_OPTIONS)
        if unknown:
            self.error(
                'Unknown output options: %s' %
                ','.join(unknown)
            )


class ParseError(Exception):
    pass


class KeyValue(object):
    """Base key-value pair parsed from CLI."""

    def __init__(self, key, value, sep, orig):
        self.key = key
        self.value = value
        self.sep = sep
        self.orig = orig

    def __eq__(self, other):
        return self.__dict__ == other.__dict__


class KeyValueArgType(object):
    """
    A key-value pair argument type used with `argparse`.

    Parses a key-value arg and constructs a `KeyValue` instance.
    Used for headers, form data, and other key-value pair types.

    """

    key_value_class = KeyValue

    def __init__(self, *separators):
        self.separators = separators

    def __call__(self, string):
        """
        Parse `string` and return `self.key_value_class()` instance.

        The best of `self.separators` is determined (first found, longest).
        Back slash escaped characters aren't considered as separators
        (or parts thereof). Literal back slash characters have to be escaped
        as well (r'\\').

        """

        class Escaped(str):
            pass

        def tokenize(s):
            """
            r'foo\=bar\\baz'
            => ['foo', Escaped('='), 'bar', Escaped('\'), 'baz']

            """
            tokens = ['']
            esc = False
            for c in s:
                if esc:
                    tokens.extend([Escaped(c), ''])
                    esc = False
                else:
                    if c == '\\':
                        esc = True
                    else:
                        tokens[-1] += c
            return tokens

        tokens = tokenize(string)

        # Sorting by length ensures that the longest one will be
        # chosen as it will overwrite any shorter ones starting
        # at the same position in the `found` dictionary.
        separators = sorted(self.separators, key=len)

        for i, token in enumerate(tokens):

            if isinstance(token, Escaped):
                continue

            found = {}
            for sep in separators:
                pos = token.find(sep)
                if pos != -1:
                    found[pos] = sep

            if found:
                # Starting first, longest separator found.
                sep = found[min(found.keys())]

                key, value = token.split(sep, 1)

                # Any preceding tokens are part of the key.
                key = ''.join(tokens[:i]) + key

                # Any following tokens are part of the value.
                value += ''.join(tokens[i + 1:])

                break

        else:
            raise argparse.ArgumentTypeError(
                '"%s" is not a valid value' % string)

        return self.key_value_class(
            key=key, value=value, sep=sep, orig=string)


class AuthCredentials(KeyValue):
    """
    Represents parsed credentials.

    """
    def _getpass(self, prompt):
        # To allow mocking.
        return getpass.getpass(prompt)

    def has_password(self):
        return self.value is not None

    def prompt_password(self):
        try:
            self.value = self._getpass("Password for user '%s': " % self.key)
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write('\n')
            sys.exit(0)


class AuthCredentialsArgType(KeyValueArgType):

    key_value_class = AuthCredentials

    def __call__(self, string):
        try:
            return super(AuthCredentialsArgType, self).__call__(string)
        except argparse.ArgumentTypeError:
            # No password provided, will prompt for it later.
            return self.key_value_class(
                key=string,
                value=None,
                sep=SEP_COMMON,
                orig=string
            )


class ParamDict(OrderedDict):

    def __setitem__(self, key, value):
        """
        If `key` is assigned more than once, `self[key]` holds a
        `list` of all the values.

        This allows having multiple fields with the same name in form
        data and URL params.

        """
        # NOTE: Won't work when used for form data with multiple values
        # for a field and a file field is present:
        # https://github.com/kennethreitz/requests/issues/737
        if key not in self:
            super(ParamDict, self).__setitem__(key, value)
        else:
            if not isinstance(self[key], list):
                super(ParamDict, self).__setitem__(key, [self[key]])
            self[key].append(value)


def parse_items(items, data=None, headers=None, files=None, params=None):
    """
    Parse `KeyValue` `items` into `data`, `headers`, `files`,
    and `params`.

    """
    if headers is None:
        headers = {}
    if data is None:
        data = {}
    if files is None:
        files = {}
    if params is None:
        params = ParamDict()
    for item in items:
        value = item.value
        key = item.key
        if item.sep == SEP_HEADERS:
            target = headers
        elif item.sep == SEP_QUERY:
            target = params
        elif item.sep == SEP_FILES:
            try:
                value = open(os.path.expanduser(item.value), 'r')
            except IOError as e:
                raise ParseError(
                    'Invalid argument %r. %s' % (item.orig, e))
            if not key:
                key = os.path.basename(value.name)
            target = files
        elif item.sep in [SEP_DATA, SEP_DATA_RAW_JSON]:
            if item.sep == SEP_DATA_RAW_JSON:
                try:
                    value = json.loads(item.value)
                except ValueError:
                    raise ParseError('%s is not valid JSON' % item.orig)
            target = data
        else:
            raise ParseError('%s is not valid item' % item.orig)

        if key in target:
            ParseError('duplicate item %s (%s)' % (item.key, item.orig))

        target[key] = value

    return headers, data, files, params
