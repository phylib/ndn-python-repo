import asyncio as aio
import logging
import struct
import time
from hashlib import sha256
from os import urandom
from random import SystemRandom
from typing import Optional
from base64 import b64decode

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad, unpad
from Cryptodome.Hash import SHA256
from Cryptodome.Signature import DSS
from Cryptodome.PublicKey import ECC

from ndn.app import NDNApp
from ndn.encoding import Name, InterestParam, NonStrictName, DecodeError, FormalName, SignaturePtrs, SignatureType, parse_and_check_tl
from ndn.types import InterestNack, InterestTimeout
from . import ReadHandle, CommandHandle
from ..command.repo_commands import RepoCommandParameter, RepoCommandResponse
from ..utils import concurrent_fetcher, PubSub, EncryptedPack, EncryptedContent, TLV_ENCRYPTED_CONTENT_TYPE
from ..storage import Storage
from typing import Optional


class WriteCommandHandle(CommandHandle):
    """
    WriteCommandHandle processes insert command interests, and fetches corresponding data to
    store them into the database.
    TODO: Add validator
    """
    def __init__(self, app: NDNApp, storage: Storage, pb: PubSub, read_handle: ReadHandle,
                 config: dict, insert_callback: None):
        """
        Write handle need to keep a reference to write handle to register new prefixes.

        :param app: NDNApp.
        :param storage: Storage.
        :param read_handle: ReadHandle. This param is necessary, because WriteCommandHandle need to
            call ReadHandle.listen() to register new prefixes.
        """
        super(WriteCommandHandle, self).__init__(app, storage, pb, config)
        self.m_read_handle = read_handle
        self.prefix = None
        self.register_root = config['repo_config']['register_root']
        self.content_key_path = config['security_config']['content_key']['path']

        with open(self.content_key_path) as fp:
            self.content_key_name = fp.readline()[:-1]
            self.content_key_bits = b64decode(fp.readline())

        self.insert_callback = insert_callback

    async def listen(self, prefix: NonStrictName):
        """
        Register routes for command interests.
        This function needs to be called explicitly after initialization.

        :param prefix: NonStrictName. The name prefix to listen on.
        """
        self.prefix = prefix

        # subscribe to insert messages
        self.pb.subscribe(self.prefix + ['insert'], self._on_insert_msg)

        # listen on insert check interests
        self.app.route(self.prefix + ['insert check'])(self._on_check_interest)

    def _on_insert_msg(self, msg):
        try:
            cmd_param = RepoCommandParameter.parse(msg)
            if cmd_param.name == None:
                raise DecodeError()
        except (DecodeError, IndexError) as exc:
            logging.warning('Parameter interest blob decoding failed')
            return
        aio.ensure_future(self._process_insert(cmd_param))

    async def _process_insert(self, cmd_param: RepoCommandParameter):
        """
        Process segmented insertion command.
        Return to client with status code 100 immediately, and then start data fetching process.
        """
        try:
            name = cmd_param.name
            start_block_id = cmd_param.start_block_id
            end_block_id = cmd_param.end_block_id
            process_id = cmd_param.process_id
            if cmd_param.register_prefix:
                register_prefix = cmd_param.register_prefix.name
            else:
                register_prefix = None
            # support only 1 forwarding hint now
            if cmd_param.forwarding_hint and cmd_param.forwarding_hint.name:
                forwarding_hint = [(0x0, cmd_param.forwarding_hint.name)]
            else:
                forwarding_hint = None
            check_prefix = cmd_param.check_prefix.name
        except AttributeError:
            return

        logging.info(f'Write handle processing insert command: {Name.to_str(name)}, first block: {start_block_id}, last block: {end_block_id}')

        # rejects any data that overlaps with repo's own namespace
        if Name.is_prefix(self.prefix, name) or Name.is_prefix(name, self.prefix):
            logging.warning('Inserted data name overlaps with repo prefix')
            return
        elif self.normalize_params_or_reject(cmd_param) == False:
            logging.warning('Insert command malformed')
            return

        # Reply to client with status code 100
        self.m_processes[process_id] = RepoCommandResponse()
        self.m_processes[process_id].process_id = process_id
        self.m_processes[process_id].insert_num = 0

        # Remember the prefixes to register
        # if register_prefix:
        #     is_existing = CommandHandle.add_registered_prefix_in_storage(self.storage, register_prefix)
        #     # If repo does not register root prefix, the client tells repo what to register
        #     if not self.register_root and not is_existing:
        #         self.m_read_handle.listen(register_prefix)
        #
        # # Remember the files inserted, this is useful for enumerating all inserted files
        # CommandHandle.add_inserted_filename_in_storage(self.storage, name)

        # Start data fetching process
        self.m_processes[process_id].status_code = 300
        insert_num = 0
        is_success = False
        if start_block_id != None:
            # Fetch data packets with block ids appended to the end
            insert_num = await self.fetch_segmented_data(name, start_block_id, end_block_id, forwarding_hint)
            if end_block_id is None or start_block_id + insert_num - 1 == end_block_id:
                is_success = True
        else:
            # Both start_block_id and end_block_id are None, fetch a single data packet
            insert_num = await self.fetch_single_data(name, forwarding_hint)
            if insert_num == 1:
                is_success = True

        if is_success:
            self.m_processes[process_id].status_code = 200
            logging.info('Insertion success, {} items inserted'.format(insert_num))
        else:
            self.m_processes[process_id].status_code = 400
            logging.info('Insertion failure, {} items inserted'.format(insert_num))
        self.m_processes[process_id].insert_num = insert_num

        # Delete process state after some time
        await self._delete_process_state_after(process_id, 60)

    def normalize_params_or_reject(self, cmd_param):
        """
        Normalize insert parameter, or reject the param if it's invalid.
        :param cmd_param: RepoCommandParameter.
        :return: Returns true if cmd_param is valid.
        """
        start_block_id = cmd_param.start_block_id
        end_block_id = cmd_param.end_block_id

        # Valid if neither start_block_id or end_block_id is given, fetch single data without seg number
        if start_block_id == None and end_block_id == None:
            return True

        # If start_block_id is not given, it is set to 0
        if start_block_id == None:
            cmd_param.start_block_id = 0

        # Valid if end_block_id is not given, attempt to fetch all segments until receiving timeout
        # Valid if end_block_id is given, and larger than or equal to start_block_id
        if end_block_id == None or end_block_id >= start_block_id:
            return True
        
        return False

    async def fetch_single_data(self, name: NonStrictName, forwarding_hint: Optional[NonStrictName]):
        """
        Fetch one Data packet.
        :param name: NonStrictName.
        :return:  Number of data packets fetched.
        """
        try:
            data_name, _, content, data_bytes = await self.app.express_interest(
                name, need_raw_packet=True, can_be_prefix=False, lifetime=1000,
                forwarding_hint=forwarding_hint)
            if self.insert_callback != None:
                self.insert_callback(data_name, content)
        except InterestNack as e:
            logging.info(f'Nacked with reason={e.reason}')
            return 0
        except InterestTimeout:
            logging.info(f'Timeout')
            return 0
        # self.storage.put_data_packet(data_name, data_bytes)
        return 1

    async def fetch_segmented_data(self, name, start_block_id: int, end_block_id: Optional[int],
                                   forwarding_hint: Optional[NonStrictName]):
        """
        Fetch segmented Data packets.
        :param name: NonStrictName.
        :return: Number of data packets fetched.
        """
        semaphore = aio.Semaphore(10)
        block_id = start_block_id
        b_array = bytearray()
        #Todo: Verify signature in concurrent_fetcher, or later in loop.
        async for (data_name, _, content, data_bytes) in concurrent_fetcher(self.app, name, start_block_id, end_block_id, semaphore, forwarding_hint=forwarding_hint,
                                                                            validator=self.verify_signature):
            # Decryption
            logging.debug(f'Received Data content size', len(content))
            pack_bytes = parse_and_check_tl(bytes(content), TLV_ENCRYPTED_CONTENT_TYPE)
            pack = EncryptedPack.parse(pack_bytes)
            cipher = AES.new(self.content_key_bits, AES.MODE_CBC, pack.iv)
            payload = unpad(cipher.decrypt(bytes(pack.payload)), 16)
            b_array.extend(payload)
            block_id += 1

        if self.insert_callback != None:
            self.insert_callback(name, b_array)

        insert_num = block_id - start_block_id
        return insert_num

    async def verify_signature(self, name: FormalName, sig: SignaturePtrs) -> bool:
        sig_info = sig.signature_info
        covered_part = sig.signature_covered_part
        sig_value = sig.signature_value_buf
        if not sig_info or sig_info.signature_type != SignatureType.SHA256_WITH_ECDSA:
            return False
        if not covered_part or not sig_value:
            return False
        identity = [sig_info.key_locator.name[0]]
        logging.debug('Extract identity id from key id: %s', Name.to_str(identity))
        key_bits = None
        try:
            key_bits = self.app.keychain.get(identity).default_key().key_bits
        except (KeyError, AttributeError):
            logging.error('Cannot find pub key from keychain')
            return False
        pk = ECC.import_key(key_bits)
        verifier = DSS.new(pk, 'fips-186-3', 'der')
        sha256_hash = SHA256.new()
        for blk in covered_part:
            sha256_hash.update(blk)
        logging.debug(bytes(sig_value))
        logging.debug(len(bytes(sig_value)))
        try:
            verifier.verify(sha256_hash, bytes(sig_value))
        except ValueError:
            return False
        logging.debug('Pass the signature verification')
        return True