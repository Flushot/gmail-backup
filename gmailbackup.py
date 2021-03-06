#!/usr/bin/env python
"""
Gmail Backup

http://github.com/Flushot/gmail-backup

Copyright 2013 Chris Lyon

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import print_function

import os
import sys
import email
import imaplib
import logging
import getpass
import hashlib
import math

import argparse

__version__ = '1.0.2'


# Gmail server
DEFAULT_IMAP_HOST = 'imap.gmail.com'
DEFAULT_IMAP_PORT = 993
DEFAULT_LABEL = '[Gmail]/All Mail'
DEFAULT_FORMAT = '(RFC822)'

SUCCESS = 0
FAILURE = 1

log = logging.getLogger(__name__)


class MailboxIterator(object):
    """
    Iterates a mailbox, yielding tuples of (unique_message_key, raw_message_body)
    """
    def __init__(self, client, mailbox=DEFAULT_LABEL, search_query='ALL', format=DEFAULT_FORMAT, key=None):
        """
        Parameters
        ----------
        client : IMAP4
            IMAP4 client to use for iteration.
        mailbox : str
            Name of the mailbox to open.
        search_query : str
            Optional search query (defaults to 'ALL')
        key : function(message)
            Message ID hash function
        """
        self._client = client

        if not isinstance(mailbox, basestring):
            raise ValueError('mailbox must be a string')
        self.mailbox = mailbox

        if not isinstance(search_query, basestring):
            raise ValueError('search_query must be a string')
        self.search_query = search_query

        if not isinstance(format, basestring):
            raise ValueError('format must be a string')
        self.format = format

        if key is not None:
            if not callable(key):
                raise ValueError('key must be callable')
            self.key = key

        self._ids = []
        self._id_iter = None

        self.reset()

    def __iter__(self):
        return self

    def key(self, raw_message):
        """
        SHA-256 hash of the entire email message.
        Previous method of hashing the Message-ID was flawed because it may not be present in all cases.
        """
        if self.format != DEFAULT_FORMAT:
            raise ValueError('key function must be set if format is not default')

        #message = email.message_from_string(raw_message)
        return hashlib.sha256(raw_message).hexdigest()

    def close(self):
        """
        Close mailbox for this iterator.
        """
        log.debug('Closing mailbox: %s' % self.mailbox)
        self._client.close()

    def reset(self):
        """
        Reset the iterator (re-opens mailbox)
        """
        # Close previously opened mailbox
        if self._ids:
            self._client.close()

        # Open mailbox
        log.debug('Opening mailbox: %s' % self.mailbox)
        self._client.select(self.mailbox, readonly=True)
        typ, ids = self._client.search(None, self.search_query)

        # Set iterator
        self._ids = ids[0].split()
        self._id_iter = iter(self._ids)

    def next(self):
        """
        Get the next message from this mailbox
        """
        try:
            message_id = self._id_iter.next()

            # Download message in RFC822 (*.eml file) format
            typ, data = self._client.fetch(message_set=message_id,
                                           message_parts=self.format)
            raw_message = data[0][1]  # First message part, content portion of tuple
            return self.key(raw_message), raw_message

        except StopIteration, ex:
            self.close()
            raise ex

    def __next__(self):
        return self.next()

    @property
    def total_messages(self):
        """
        Total number of messages available in this mailbox.
        """
        return len(self._ids)


class GmailClientException(Exception):
    def __init__(self, message, inner_ex):
        super(GmailClientException, self).__init__(message, inner_ex)


class GmailClient(object):
    def __init__(self, host=DEFAULT_IMAP_HOST, port=DEFAULT_IMAP_PORT):
        self.host = host
        self.port = port
        self._connected = False
        self._authenticated = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, typ, value, tb):
        self.close()

    def connect(self):
        # Connect to server
        self._client = imaplib.IMAP4_SSL(self.host, self.port)
        self._connected = True

    def close(self):
        assert self._connected

        log.debug('Logging out...')
        self._client.logout()
        self._connected = False
        self._authenticated = False

    def authenticate(self, username, password):
        """
        Login to Gmail

        username : str
        password : str
        """
        assert self._connected
        
        # Determine authentication method
        if 'AUTH=CRAM-MD5' in self._client.capabilities:
            # Prefer CRAM-MD5 if supported
            login_method = self._client.login_cram_md5
        else:
            # Fallback
            login_method = self._client.login

        # Authenticate
        try:
            login_method(username, password)
            self._authenticated = True
            log.debug('Logged in as %s' % username)

        except imaplib.IMAP4.error, ex:
            # Probably a bad password
            error = 'Authentication error: %s' % ex.message
            log.error(error)
            raise GmailClientException(error, ex)

    @property
    def is_authenticated(self):
        return self._authenticated

    def iter_mailbox(self, mailbox):
        """
        Opens a mailbox named :mailbox:, returning a MailboxIterator.
        """
        assert self.is_authenticated

        return MailboxIterator(self._client, mailbox)

    def save_mailbox(self, mailbox, output_path, progress_updated=None):
        """
        Downloads and saves email messages in a :mailbox: as *.eml files in :output_path: directory.

        Parameters
        ----------
        mailbox : str
            Name of the mailbox to save.
        output_path : str
            Directory to save *.eml files to.
        """
        assert self.is_authenticated
        
        # Iterate messages
        download_count = 0
        mailbox_iterator = self.iter_mailbox(mailbox)
        for message_key, raw_message in mailbox_iterator:

            # Write eml file
            email_file = os.path.join(output_path, '%s.eml' % message_key)
            with open(email_file, 'w+') as f:
                f.write(raw_message)

            download_count += 1
            percent_complete = download_count / (mailbox_iterator.total_messages / 100.0)
            log.debug('%s - Downloaded message %s (%.2f%% complete)' % (
                      mailbox, message_key, percent_complete))
            if progress_updated is not None:
                progress_updated(message_key, percent_complete)


def ensure_dir_exists(path):
    if not os.path.exists(path):
        os.mkdir(path)


def update_progress(percent, prefix=None):
    max_bars = 10
    bars = ('#' * int(math.floor(percent / max_bars))).ljust(max_bars, ' ')

    if prefix is None:
        prefix_str = ''
    else:
        prefix_str = prefix + ' '

    sys.stdout.write('\r%s[%s] %.2f%% ' % (prefix_str, bars, percent))
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description='Gmail backup tool')
    parser.add_argument('-u', '--username', metavar='username', required=True,
                        help='Gmail username')
    parser.add_argument('-p', '--password', metavar='password', 
                        help='Gmail password')
    parser.add_argument('-o', '--output-path', metavar='directory', default='email',
                        help='Output directory where *.eml files will be downloaded to')
    parser.add_argument('-l', '--labels', metavar='label1,labelN,...',
                        help='Comma-separated list of labels to download (when omitted, downloads all mail)')
    parser.add_argument('--imap-host', metavar='hostname', default=DEFAULT_IMAP_HOST,
                        help='IMAP server for Gmail (default is %s)' % DEFAULT_IMAP_HOST)
    parser.add_argument('--imap-port', metavar='port', type=int, default=DEFAULT_IMAP_PORT,
                        help='IMAP server port for Gmail (default is %d)' % DEFAULT_IMAP_PORT)
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug mode')
    args = vars(parser.parse_args())

    # Init logging
    logging.basicConfig(
        level=logging.DEBUG if args['debug'] else logging.WARN,
        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
        datefmt='%m-%d %H:%M')

    # Get username and password
    username = args['username']
    password = args.get('password')
    if not password:
        # Prompt for password, since it wasn't specified as an arg
        password = getpass.getpass('Password for %s: ' % username)
        if not password:
            print('Password is required!', file=sys.stderr)
            sys.exit(FAILURE)

    # Directory *.eml files are stored in
    output_path = args['output_path']
    ensure_dir_exists(output_path)

    try:
        with GmailClient(args['imap_host'], args['imap_port']) as client:
            client.authenticate(username, password)

            # Iterate labels
            labels = map(lambda x: x.strip(), (args.get('labels') or DEFAULT_LABEL).split(','))
            for label in labels:

                # Determine download path
                if label == DEFAULT_LABEL:
                    label_path = 'All Mail'
                else:
                    label_path = label
                label_output_path = os.path.join(output_path, label_path)
                ensure_dir_exists(label_output_path)

                client.save_mailbox(label, label_output_path, 
                                    progress_updated=lambda key, percent: \
                                        update_progress(percent, label_path + ' ' + key))

                sys.stdout.write('\n')

            sys.exit(SUCCESS)

    except KeyboardInterrupt, ex:
        sys.stdout.write('\n')
        if args['debug']:
            # Full stack trace is useful
            raise ex
        else:
            print('^C pressed. Terminating...')

    sys.exit(FAILURE)


if __name__ == '__main__':
    main()
