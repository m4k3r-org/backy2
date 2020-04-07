#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from backy2.data_backends import DataBackend as _DataBackend
from backy2.logging import logger
from backy2.utils import TokenBucket
import boto3
from botocore.client import Config as BotoCoreClientConfig
from botocore.exceptions import ClientError
from botocore.handlers import set_list_objects_encoding_type_url
import hashlib
import os
import queue
import shortuuid
import socket
import threading
import time

STATUS_NOTHING = 0
STATUS_READING = 1
STATUS_WRITING = 2
STATUS_THROTTLING = 3
STATUS_NEWKEY = 4

class DataBackend(_DataBackend):
    """ A DataBackend which stores in S3 compatible storages. The files are
    stored in a configurable bucket. """

    WRITE_QUEUE_LENGTH = 20
    READ_QUEUE_LENGTH = 20

    _SUPPORTS_PARTIAL_READS = False
    _SUPPORTS_PARTIAL_WRITES = False
    fatal_error = None

    def __init__(self, config):
        aws_access_key_id = config.get('aws_access_key_id')
        if aws_access_key_id is None:
            aws_access_key_id_file = config.get('aws_access_key_id_file')
            with open(aws_access_key_id_file, 'r', encoding="ascii") as f:
                aws_access_key_id = f.read().rstrip()

        aws_secret_access_key = config.get('aws_secret_access_key')
        if aws_secret_access_key is None:
            aws_secret_access_key_file = config.get('aws_secret_access_key_file')
            with open(aws_secret_access_key_file, 'r', encoding="ascii") as f:
                aws_secret_access_key = f.read().rstrip()

        region_name = config.get('region_name', '')
        endpoint_url = config.get('endpoint_url', '')
        use_ssl = config.get('use_ssl', '')
        self._bucket_name = config.get('bucket_name', '')
        addressing_style = config.get('addressing_style', '')
        signature_version = config.get('signature_version', '')
        self._disable_encoding_type = config.get('disable_encoding_type', '')

        simultaneous_writes = config.getint('simultaneous_writes', 1)
        simultaneous_reads = config.getint('simultaneous_reads', 1)
        bandwidth_read = config.getint('bandwidth_read', 0)
        bandwidth_write = config.getint('bandwidth_write', 0)

        self.read_throttling = TokenBucket()
        self.read_throttling.set_rate(bandwidth_read)  # 0 disables throttling
        self.write_throttling = TokenBucket()
        self.write_throttling.set_rate(bandwidth_write)  # 0 disables throttling


        self._resource_config = {
            'aws_access_key_id': aws_access_key_id,
            'aws_secret_access_key': aws_secret_access_key,
        }

        if region_name:
            self._resource_config['region_name'] = region_name

        if endpoint_url:
            self._resource_config['endpoint_url'] = endpoint_url

        if use_ssl:
            self._resource_config['use_ssl'] = use_ssl

        resource_config = {}
        if addressing_style:
            resource_config['s3'] = {'addressing_style': addressing_style}

        if signature_version:
            resource_config['signature_version'] = signature_version

        self._resource_config['config'] = BotoCoreClientConfig(**resource_config)


        self.write_queue_length = simultaneous_writes + self.WRITE_QUEUE_LENGTH
        self.read_queue_length = simultaneous_reads + self.READ_QUEUE_LENGTH
        self._write_queue = queue.Queue(self.write_queue_length)
        self._read_queue = queue.Queue()
        self._read_data_queue = queue.Queue(self.read_queue_length)

        self.bucket = self._get_bucket()  # for read_raw

        self._writer_threads = []
        self._reader_threads = []
        self.reader_thread_status = {}
        self.writer_thread_status = {}
        for i in range(simultaneous_writes):
            _writer_thread = threading.Thread(target=self._writer, args=(i,))
            _writer_thread.daemon = True
            _writer_thread.start()
            self._writer_threads.append(_writer_thread)
            self.writer_thread_status[i] = STATUS_NOTHING
        for i in range(simultaneous_reads):
            _reader_thread = threading.Thread(target=self._reader, args=(i,))
            _reader_thread.daemon = True
            _reader_thread.start()
            self._reader_threads.append(_reader_thread)
            self.reader_thread_status[i] = STATUS_NOTHING


    def _get_bucket(self):
        session = boto3.session.Session()
        if self._disable_encoding_type:
            session.events.unregister('before-parameter-build.s3.ListObjects', set_list_objects_encoding_type_url)
        resource = session.resource('s3', **self._resource_config)
        bucket = resource.Bucket(self._bucket_name)
        return bucket


    def _writer(self, id_):
        """ A threaded background writer """
        bucket = None
        while True:
            entry = self._write_queue.get()
            if entry is None or self.fatal_error:
                logger.debug("Writer {} finishing.".format(id_))
                break
            if bucket is None:
                bucket = self._get_bucket()
            uid, data = entry

            self.writer_thread_status[id_] = STATUS_THROTTLING
            time.sleep(self.write_throttling.consume(len(data)))
            self.writer_thread_status[id_] = STATUS_NOTHING

            t1 = time.time()

            self.writer_thread_status[id_] = STATUS_NEWKEY
            obj = bucket.Object(uid)
            self.writer_thread_status[id_] = STATUS_NOTHING

            self.writer_thread_status[id_] = STATUS_WRITING
            obj.put(Body=data)
            self.writer_thread_status[id_] = STATUS_NOTHING

            t2 = time.time()

            self._write_queue.task_done()
            logger.debug('Writer {} with bucket {} wrote data {} in {:.2f}s (Queue size is {})'.format(id_, id(bucket), uid, t2-t1, self._write_queue.qsize()))


    def _reader(self, id_):
        """ A threaded background reader """
        bucket = None
        while True:
            block = self._read_queue.get()  # contains block
            if block is None or self.fatal_error:
                logger.debug("Reader {} finishing.".format(id_))
                break
            if bucket is None:
                bucket = self._get_bucket()
            t1 = time.time()
            try:
                self.reader_thread_status[id_] = STATUS_READING
                data = self.read_raw(block.uid, bucket)
                self.reader_thread_status[id_] = STATUS_NOTHING
            except FileNotFoundError:
                self._read_data_queue.put((block, None))  # TODO: catch this!
            else:
                self._read_data_queue.put((block, data))
                t2 = time.time()
                self._read_queue.task_done()
                logger.debug('Reader {} read data async. uid {} in {:.2f}s (Queue size is {})'.format(id_, block.uid, t2-t1, self._read_queue.qsize()))


    def read_raw(self, block_uid, _bucket=None):
        if not _bucket:
            _bucket = self.bucket

        while True:
            obj = _bucket.Object(block_uid)
            try:
                data_dict = obj.get()
                data = data_dict['Body'].read()
            except ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchKey' or e.response['Error']['Code'] == '404':
                    raise FileNotFoundError('Key {} not found.'.format(key)) from None
                else:
                    raise
            except socket.timeout:
                logger.error('Timeout while fetching from s3, trying again.')
                pass
            except OSError as e:
                # TODO: This is new and currently untested code. I'm not sure
                # why this happens in favour of socket.timeout and also if it
                # might be better to abort the whole restore/backup/scrub if
                # this happens, because I can't tell if the s3 lib is able to
                # recover from this situation and continue or not. We will see
                # this in the logs next time s3 is generating timeouts.
                logger.error('Timeout while fetching from s3 - error is "{}", trying again.'.format(str(e)))
                pass
            else:
                break
        time.sleep(self.read_throttling.consume(len(data)))  # TODO: Need throttling in thread statistics!
        return data


    def _uid(self):
        # 32 chars are allowed and we need to spread the first few chars so
        # that blobs are distributed nicely. And want to avoid hash collisions.
        # So we create a real base57-encoded uuid (22 chars) and prefix it with
        # its own md5 hash[:10].
        suuid = shortuuid.uuid()
        hash = hashlib.md5(suuid.encode('ascii')).hexdigest()
        return hash[:10] + suuid


    def save(self, data, _sync=False):
        if self.fatal_error:
            raise self.fatal_error
        uid = self._uid()
        self._write_queue.put((uid, data))
        if _sync:
            self._write_queue.join()
        return uid


    def rm(self, uid):
        obj = self.bucket.Object(uid)
        try:
            obj.load()
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey' or e.response['Error']['Code'] == '404':
                raise FileNotFoundError('Key {} not found.'.format(uid)) from None
            else:
                raise
        else:
            obj.delete()


    def rm_many(self, uids):
        """ Deletes many uids from the data backend and returns a list
        of uids that couldn't be deleted.
        """
        for uid in uids:
            self.rm(uid)
        # TODO: maybe use delete_objects


    def read(self, block, sync=False):
        self._read_queue.put(block)
        if sync:
            rblock, offset, length, data = self.read_get()
            if rblock.id != block.id:
                raise RuntimeError('Do not mix threaded reading with sync reading!')
            if data is None:
                raise FileNotFoundError('UID {} not found.'.format(block.uid))
            return data


    def read_get(self):
        block, data = self._read_data_queue.get()
        offset = 0
        length = len(data)
        self._read_data_queue.task_done()
        return block, offset, length, data


    def read_queue_size(self):
        return self._read_queue.qsize()


    def get_all_blob_uids(self, prefix=None):
        if prefix is None:
            objects_iterable = self.bucket.objects.all()
        else:
            objects_iterable = self.bucket.objects.filter(Prefix=prefix)

        return [o.key for o in objects_iterable]


    def queue_status(self):
        return {
            'rq_filled': self._read_data_queue.qsize() / self._read_data_queue.maxsize,  # 0..1
            'wq_filled': self._write_queue.qsize() / self._write_queue.maxsize,
        }


    def thread_status(self):
        return "DaBaR: N{} R{} QL{}  DaBaW: N{} W{} T{} QL{}".format(
                len([t for t in self.reader_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.reader_thread_status.values() if t==STATUS_READING]),
                self._read_queue.qsize(),
                len([t for t in self.writer_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_WRITING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_THROTTLING]),
                self._write_queue.qsize(),
                )


    def close(self):
        for _writer_thread in self._writer_threads:
            self._write_queue.put(None)  # ends the thread
        for _writer_thread in self._writer_threads:
            _writer_thread.join()
        for _reader_thread in self._reader_threads:
            self._read_queue.put(None)  # ends the thread
        for _reader_thread in self._reader_threads:
            _reader_thread.join()


