#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from backy2.logging import logger
from backy2.io import IO as _IO
from collections import namedtuple
import os
import queue
import re
import threading
import time

STATUS_NOTHING = 0
STATUS_READING = 1
STATUS_WRITING = 2
STATUS_SEEKING = 3
STATUS_FADVISE = 4

if hasattr(os, 'posix_fadvise'):
    posix_fadvise = os.posix_fadvise
else:  # pragma: no cover
    logger.warn('Running without `posix_fadvise`.')
    os.POSIX_FADV_RANDOM = None
    os.POSIX_FADV_SEQUENTIAL = None
    os.POSIX_FADV_WILLNEED = None
    os.POSIX_FADV_DONTNEED = None

    def posix_fadvise(*args, **kw):
        return


class IO(_IO):
    mode = None
    _write_file = None
    WRITE_QUEUE_LENGTH = 20
    READ_QUEUE_LENGTH = 20

    def __init__(self, config, block_size, hash_function):
        self.simultaneous_reads = config.getint('simultaneous_reads', 1)
        self.simultaneous_writes = config.getint('simultaneous_reads', 1)
        self.block_size = block_size
        self.hash_function = hash_function

        self._reader_threads = []
        self._writer_threads = []

        self.reader_thread_status = {}
        self.writer_thread_status = {}


    def open_r(self, io_name):
        self.mode = 'r'
        _s = re.match('^file://(.+)$', io_name)
        if not _s:
            raise RuntimeError('Not a valid io name: {} . Need a file path, e.g. file:///somepath/file'.format(io_name))
        self.io_name = _s.groups()[0]

        self._inqueue = queue.Queue()  # infinite size for all the blocks
        self._outqueue = queue.Queue(self.simultaneous_reads + self.READ_QUEUE_LENGTH)  # data of read blocks
        for i in range(self.simultaneous_reads):
            _reader_thread = threading.Thread(target=self._reader, args=(i,))
            _reader_thread.daemon = True
            _reader_thread.start()
            self._reader_threads.append(_reader_thread)
            self.reader_thread_status[i] = STATUS_NOTHING


    def open_w(self, io_name, size=None, force=False):
        # parameter size is version's size.
        self.mode = 'w'
        _s = re.match('^file://(.+)$', io_name)
        if not _s:
            raise RuntimeError('Not a valid io name: {} . Need a file path, e.g. file:///somepath/file'.format(io_name))
        self.io_name = _s.groups()[0]

        if os.path.exists(self.io_name):
            if not force:
                logger.error('Target already exists: {}'.format(io_name))
                exit('Error opening restore target. You must force the restore.')
            else:
                if self.size() < size:
                    logger.error('Target size is too small. Has {}b, need {}b.'.format(self.size(), size))
                    exit('Error opening restore target.')
        else:
            # create the file
            with open(self.io_name, 'wb') as f:
                f.seek(size - 1)
                f.write(b'\0')

        self._write_file = open(self.io_name, 'rb+')

        self._write_queue = queue.Queue(self.simultaneous_writes + self.WRITE_QUEUE_LENGTH)  # blocks to be written
        for i in range(self.simultaneous_writes):
            _writer_thread = threading.Thread(target=self._writer, args=(i,))
            _writer_thread.daemon = True
            _writer_thread.start()
            self._writer_threads.append(_writer_thread)
            self.writer_thread_status[i] = STATUS_NOTHING


    def size(self):
        source_size = 0
        with open(self.io_name, 'rb') as source_file:
            #posix_fadvise(source_file.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
            # determine source size
            source_file.seek(0, 2)  # to the end
            source_size = source_file.tell()
            source_file.seek(0)
        return source_size


    def _writer(self, id_):
        """ self._write_queue contains a list of (Block, data) to be written.
        """
        while True:
            entry = self._write_queue.get()
            if entry is None:
                logger.debug("IO writer {} finishing.".format(id_))
                break
            block, data = entry

            offset = block.id * self.block_size

            self.writer_thread_status[id_] = STATUS_SEEKING
            self._write_file.seek(offset)

            self.writer_thread_status[id_] = STATUS_WRITING
            written = self._write_file.write(data)

            self.writer_thread_status[id_] = STATUS_NOTHING
            posix_fadvise(self._write_file.fileno(), offset, offset + self.block_size, os.POSIX_FADV_DONTNEED)
            assert written == len(data)

            self._write_queue.task_done()


    def _reader(self, id_):
        """ self._inqueue contains Blocks to be read.
        self._outqueue contains (block, data, data_checksum)
        """
        with open(self.io_name, 'rb') as source_file:
            while True:
                block = self._inqueue.get()
                if block is None:
                    logger.debug("IO {} finishing.".format(id_))
                    self._outqueue.put(None)  # also let the outqueue end
                    break
                offset = block.id * self.block_size
                t1 = time.time()
                self.reader_thread_status[id_] = STATUS_SEEKING
                source_file.seek(offset)
                t2 = time.time()
                self.reader_thread_status[id_] = STATUS_READING
                data = source_file.read(self.block_size)
                t3 = time.time()
                # throw away cache
                self.reader_thread_status[id_] = STATUS_FADVISE
                posix_fadvise(source_file.fileno(), offset, offset + self.block_size, os.POSIX_FADV_DONTNEED)
                self.reader_thread_status[id_] = STATUS_NOTHING
                if not data:
                    raise RuntimeError('EOF reached on source when there should be data.')

                data_checksum = self.hash_function(data).hexdigest()
                if not block.valid:
                    logger.debug('IO {} re-read block (because it was invalid) {} (checksum {})'.format(id_, block.id, data_checksum))
                else:
                    logger.debug('IO {} read block {} (len {}, checksum {}...) in {:.2f}s (seek in {:.2f}s) (Inqueue size: {}, Outqueue size: {})'.format(id_, block.id, len(data), data_checksum[:16], t3-t1, t2-t1, self._inqueue.qsize(), self._outqueue.qsize()))

                self._outqueue.put((block, data, data_checksum))
                self._inqueue.task_done()


    def read(self, block, sync=False):
        """ Adds a read job """
        self._inqueue.put(block)
        if sync:
            rblock, data, data_checksum = self.get()
            if rblock.id != block.id:
                raise RuntimeError('Do not mix threaded reading with sync reading!')
            return data


    def get(self):
        d = self._outqueue.get()
        self._outqueue.task_done()
        return d


    def write(self, block, data):
        """ Adds a write job"""
        # print("Writing block {} with {} bytes of data".format(block.id, len(data)))
        if not self._write_file:
            raise RuntimeError('File not open.')

        self._write_queue.put((block, data))


    def thread_status(self):
        return "IO Reader Threads: N:{} R:{} S:{} F:{}  IO Writer Threads: N:{} W:{} S:{} F:{} Queue-Length:{}".format(
                len([t for t in self.reader_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.reader_thread_status.values() if t==STATUS_READING]),
                len([t for t in self.reader_thread_status.values() if t==STATUS_SEEKING]),
                len([t for t in self.reader_thread_status.values() if t==STATUS_FADVISE]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_WRITING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_SEEKING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_FADVISE]),
                self._write_queue.qsize(),
                )


    def close(self):
        if self.mode == 'r':
            for _reader_thread in self._reader_threads:
                self._inqueue.put(None)  # ends the threads
            for _reader_thread in self._reader_threads:
                _reader_thread.join()
        elif self.mode == 'w':
            for _writer_thread in self._writer_threads:
                self._write_queue.put(None)  # ends the threads
            for _writer_thread in self._writer_threads:
                _writer_thread.join()
            self._write_file.close()

