# This file is a part of Lector, a Qt based ebook reader
# Copyright (C) 2017-2019 BasioMeusPuga

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# INSTRUCTIONS
# Every parser is supposed to have the following methods. None returns are not allowed.
# read_book() - Initialize book
# generate_metadata() - For addition
# generate_content() - For reading

import io
import os
import sys
import json
import time
import pickle
import logging
import hashlib
import threading
import importlib
import urllib.request

# The multiprocessing module does not work correctly on Windows
if sys.platform.startswith('win'):
    from multiprocessing.dummy import Pool, Manager
    thread_count = 4  # This is all on one CPU thread anyway
else:
    from multiprocessing import Pool, Manager, cpu_count
    thread_count = cpu_count()

from PyQt5 import QtCore, QtGui
from lector import database

from lector.parsers.epub import ParseEPUB
from lector.parsers.mobi import ParseMOBI
from lector.parsers.fb2 import ParseFB2
from lector.parsers.comicbooks import ParseCOMIC

logger = logging.getLogger(__name__)

sorter = {
    'cbz': ParseCOMIC,
    'cbr': ParseCOMIC}

# Check what dependencies are installed
# pymupdf - Optional
mupdf_check = importlib.util.find_spec('fitz')
if mupdf_check:
    from lector.parsers.pdf import ParsePDF
    sorter['pdf'] = ParsePDF
else:
    error_string = 'pymupdf is not installed. Will be unable to load PDFs.'
    print(error_string)
    logger.error(error_string)

# python-lxml - Required for everything except comics
lxml_check = importlib.util.find_spec('lxml')
if lxml_check:
    lxml_dependent = {
        'epub': ParseEPUB,
        'mobi': ParseMOBI,
        'azw': ParseMOBI,
        'azw3': ParseMOBI,
        'azw4': ParseMOBI,
        'prc': ParseMOBI,
        'fb2': ParseFB2,
        'fb2.zip': ParseFB2}
    sorter.update(lxml_dependent)
else:
    critical_sting = 'python-lxml is not installed. Only comics will load.'
    print(critical_sting)
    logger.critical(critical_sting)

available_parsers = [i for i in sorter]
progressbar = None  # This is populated by __main__
_progress_emitter = None  # This is to be made into a global variable


class UpdateProgress(QtCore.QObject):
    # This is for thread safety
    update_signal = QtCore.pyqtSignal(int)

    def connect_to_progressbar(self):
        self.update_signal.connect(progressbar.setValue)

    def update_progress(self, progress_percent):
        self.update_signal.emit(progress_percent)


class BookSorter:
    def __init__(self, file_list, mode, database_path, auto_tags=True, temp_dir=None):
        # Have the GUI pass a list of files straight to here
        # Then, on the basis of what is needed, pass the
        # filenames to the requisite functions
        # This includes getting file info for the database
        # Parsing for the reader proper
        # Caching upon closing
        self.file_list = [i for i in file_list if os.path.exists(i)]
        self.statistics = [0, (len(file_list))]
        self.hashes_and_paths = {}
        self.work_mode = mode[0]
        self.addition_mode = mode[1]
        self.database_path = database_path
        self.auto_tags = auto_tags
        self.temp_dir = temp_dir
        if database_path:
            self.database_hashes()

        self.threading_completed = []
        self.queue = Manager().Queue()
        self.processed_books = []

        if self.work_mode == 'addition':
            progress_object_generator()

    def database_hashes(self):
        all_hashes_and_paths = database.DatabaseFunctions(
            self.database_path).fetch_data(
                ('Hash', 'Path'),
                'books',
                {'Hash': ''},
                'LIKE')

        if all_hashes_and_paths:
            # self.hashes = [i[0] for i in all_hashes]
            self.hashes_and_paths = {
                i[0]: i[1] for i in all_hashes_and_paths}

    def database_entry_for_book(self, file_hash):
        # TODO
        # This will probably look a whole lot better with a namedtuple

        database_return = database.DatabaseFunctions(
            self.database_path).fetch_data(
                ('Title', 'Author', 'Year', 'ISBN', 'Tags',
                 'Position', 'Bookmarks', 'CoverImage', 'Annotations'),
                'books',
                {'Hash': file_hash},
                'EQUALS')[0]

        book_data = []

        for count, i in enumerate(database_return):
            if count in (5, 6, 8):  # Position, Bookmarks, and Annotations are pickled
                if i:
                    book_data.append(pickle.loads(i))
                else:
                    book_data.append(None)
            else:
                book_data.append(i)

        return book_data

    def read_book(self, filename):
        # filename is expected as a string containg the
        # full path of the ebook file

        with open(filename, 'rb') as current_book:
            # This should speed up addition for larger files
            # without compromising the integrity of the process
            first_bytes = current_book.read(1024 * 32)  # First 32KB of the file
            file_md5 = hashlib.md5(first_bytes).hexdigest()

        # Update the progress queue
        self.queue.put(filename)

        # This should not get triggered in reading mode
        # IF the file is NOT being loaded into the reader

        # Do not allow addition in case the file
        # is already in the database and it remains at its original path
        if self.work_mode == 'addition' and file_md5 in self.hashes_and_paths:
            if (self.hashes_and_paths[file_md5] == filename
                    or os.path.exists(self.hashes_and_paths[file_md5])):

                if not self.hashes_and_paths[file_md5] == filename:
                    warning_string = f'{os.path.basename(filename)} is already in database'
                    logger.warning(warning_string)
                return

        # This allows for eliminating issues with filenames that have
        # a dot in them. All hail the roundabout fix.
        valid_extension = False
        for i in sorter:
            if os.path.basename(filename).endswith(i):
                file_extension = i
                valid_extension = True
                break

        if not valid_extension:
            logger.error('Unsupported extension: ' + filename)
            return

        book_ref = sorter[file_extension](filename, self.temp_dir, file_md5)

        try:
            book_ref.read_book()
        except:
            logger.error('Error initializing: ' + filename)
            return

        this_book = {}
        this_book[file_md5] = {
            'hash': file_md5,
            'path': filename}

        # Different modes require different values
        if self.work_mode == 'addition':
            try:
                metadata = book_ref.generate_metadata()
            except:
                logger.error('Metadata generation error: ' + filename)
                return

            title = metadata.title
            author = metadata.author
            year = metadata.year
            isbn = metadata.isbn

            tags = None
            if self.auto_tags:
                tags = metadata.tags

            cover_image_raw = metadata.cover
            if cover_image_raw:
                cover_image = resize_image(cover_image_raw)
            else:
                # TODO
                # Needs an option
                # cover_image = fetch_cover(title, author)
                cover_image = None

            this_book[file_md5]['cover_image'] = cover_image
            this_book[file_md5]['addition_mode'] = self.addition_mode

        if self.work_mode == 'reading':
            try:
                book_breakdown = book_ref.generate_content()
            except:
                logger.error('Content generation error: ' + filename)
                return

            toc = book_breakdown[0]
            content = book_breakdown[1]
            images_only = book_breakdown[2]

            book_data = self.database_entry_for_book(file_md5)
            title = book_data[0]
            author = book_data[1]
            year = book_data[2]
            isbn = book_data[3]
            tags = book_data[4]
            position = book_data[5]
            bookmarks = book_data[6]
            cover = book_data[7]
            annotations = book_data[8]

            this_book[file_md5]['position'] = position
            this_book[file_md5]['bookmarks'] = bookmarks
            this_book[file_md5]['toc'] = toc
            this_book[file_md5]['content'] = content
            this_book[file_md5]['images_only'] = images_only
            this_book[file_md5]['cover'] = cover
            this_book[file_md5]['annotations'] = annotations

        this_book[file_md5]['title'] = title
        this_book[file_md5]['author'] = author
        this_book[file_md5]['year'] = year
        this_book[file_md5]['isbn'] = isbn
        this_book[file_md5]['tags'] = tags

        return this_book

    def read_progress(self):
        while True:
            processed_file = self.queue.get()
            self.threading_completed.append(processed_file)

            total_number = len(self.file_list)
            completed_number = len(self.threading_completed)

            # Just for the record, this slows down book searching by about 20%
            if _progress_emitter:  # Skip update in reading mode
                _progress_emitter.update_progress(
                    completed_number * 100 // total_number)

            if total_number == completed_number:
                break

    def initiate_threads(self):
        if not self.file_list:
            return None

        def pool_creator():
            _pool = Pool(thread_count)
            self.processed_books = _pool.map(
                self.read_book, self.file_list)

            _pool.close()
            _pool.join()

        start_time = time.time()

        worker_thread = threading.Thread(target=pool_creator)
        progress_thread = threading.Thread(target=self.read_progress)
        worker_thread.start()
        progress_thread.start()

        worker_thread.join()
        progress_thread.join(timeout=.5)

        return_books = {}
        # Exclude None returns generated in case of duplication / parse errors
        self.processed_books = [i for i in self.processed_books if i]
        for i in self.processed_books:
            for j in i:
                return_books[j] = i[j]

        del self.processed_books
        processing_time = str(time.time() - start_time)
        logger.info('Finished processing in ' + processing_time)
        return return_books


def progress_object_generator():
    # This has to be kept separate from the BookSorter class because
    # the QtObject inheritance disallows pickling
    global _progress_emitter
    _progress_emitter = UpdateProgress()
    _progress_emitter.connect_to_progressbar()


def resize_image(cover_image_raw):
    if isinstance(cover_image_raw, QtGui.QImage):
        cover_image = cover_image_raw
    else:
        cover_image = QtGui.QImage()
        cover_image.loadFromData(cover_image_raw)

    # Resize image to what literally everyone
    # agrees is an acceptable cover size
    cover_image = cover_image.scaled(
        420, 600, QtCore.Qt.IgnoreAspectRatio)

    byte_array = QtCore.QByteArray()
    buffer = QtCore.QBuffer(byte_array)
    buffer.open(QtCore.QIODevice.WriteOnly)
    cover_image.save(buffer, 'jpg', 75)

    cover_image_final = io.BytesIO(byte_array)
    cover_image_final.seek(0)
    return cover_image_final.getvalue()


def fetch_cover(title, author):
    # TODO
    # Start using the author parameter
    # Generate a cover image in case the Google API finds nothing
    # Why is that stupid UnicodeEncodeError happening?

    api_url = 'https://www.googleapis.com/books/v1/volumes?q='
    key = '&key=' + 'AIzaSyDOferpeSS424Dshs4YWY1s-nIBA9884hE'
    title = title.replace(' ', '+')
    req = api_url + title + key

    try:
        response = urllib.request.urlopen(req)
        if response.getcode() == 200:
            response_text = response.read().decode('utf-8')
            response_json = json.loads(response_text)
        else:
            return None

    except (urllib.error.HTTPError, urllib.error.URLError):
        return None

    except UnicodeEncodeError:
        logger.error('UnicodeEncodeError fetching cover for ' + title)
        return None

    try:
        # Get cover link from json
        cover_link = response_json['items'][0]['volumeInfo']['imageLinks']['thumbnail']
        # Get a slightly larger version
        cover_link = cover_link.replace('zoom=1', 'zoom=2')
        cover_request = urllib.request.urlopen(cover_link)
        response = cover_request.read()  # Bytes object
        cover_image = resize_image(response)
        logger.info('Cover found for ' + title)

        return cover_image

    except:
        logger.error(f'Couldn\'t find cover for ' + title)
        return None
