import asyncio
import functools

import async_timeout
from thriftpy.thrift import TMessageType, TApplicationException

from .protocol import TBinaryProtocol
from .util import args2kwargs
from .errors import ConnectionClosedError
from .log import logger


@asyncio.coroutine
def create_connection(service, address=('127.0.0.1', 6000), *,
                      protocol_cls=TBinaryProtocol, timeout=None, loop=None):
    """Create a thrift connection.
    This function is a coroutine.

    Open a connection to the thrift server by address argument.

    :param service: a thrift service object
    :param address: a (host, port) tuple
    :param protocol_cls: protocol type, default is `TBinaryProtocol`
    :param timeout: if specified, would raise `asyncio.TimeoutError` if one rpc call is longer
    than `timeout`
    :param loop: event loop instance, if not specified, default loop is used.
    :return: newly created connection instance.
    """
    host, port = address
    reader, writer = yield from asyncio.open_connection(
        host, port, loop=loop)
    iprotocol = protocol_cls(reader)
    oprotocol = protocol_cls(writer)

    return ThriftConnection(service, iprot=iprotocol, oprot=oprotocol,
                            address=address, loop=loop, timeout=timeout)


class ThriftConnection:
    """
    Thrift Connection.
    """

    def __init__(self, service, *, iprot, oprot, address, loop=None, timeout=None):
        self.service = service
        self._reader = iprot.trans
        self._writer = oprot.trans
        self._loop = loop
        self.timeout = timeout
        self.address = address
        self.closed = False
        self._oprot = oprot
        self._iprot = iprot
        self._seqid = 0
        self._init_rpc_apis()

    def _init_rpc_apis(self):
        """
        find out all apis defined in thrift service, and create corresponding
        method on the connection object, ignore it if some api name is conflicted with
        an existed attribute of the connection object, which you should call by using
        the `execute` method.
        """
        for api in self.service.thrift_services:
            if not hasattr(self, api):
                setattr(self, api, functools.partial(self.execute, api))
            else:
                logger.warn(
                    'api name {0} is conflicted with connection attribute '
                    '{0}, while you can still call this api by `send_call("{0}")`'.format(api))

    def __repr__(self):
        return '<ThriftConnection {} to>'.format(self.address)

    @asyncio.coroutine
    def execute(self, api, *args, **kwargs):
        """
        Execute a rpc call by api name. This is function is a coroutine.
        Raises:
            * asyncio.TimeoutError if this task has exceeded the `timeout`
            * ConnectionClosedError if server has closed this connection.
            * TApplicationException when thrift response is an exception defined in thrift.

        :param api: api name defined in thrift file
        :param args: positional arguments passed to api function
        :param kwargs:  keyword arguments passed to api function
        :return: result of this rpc call
        """
        if self.closed:
            raise ConnectionClosedError('Connection closed')

        try:
            with async_timeout.timeout(self.timeout):
                kw = args2kwargs(getattr(self.service, api + "_args").thrift_spec, *args)
                kwargs.update(kw)
                result_cls = getattr(self.service, api + "_result")

                self._seqid += 1
                self._oprot.write_message_begin(api, TMessageType.CALL, self._seqid)
                args = getattr(self.service, api + '_args')()
                for k, v in kwargs.items():
                    setattr(args, k, v)
                args.write(self._oprot)
                self._oprot.write_message_end()
                yield from self._oprot.trans.drain()
                if not getattr(result_cls, "oneway"):
                    result = yield from self._recv(api)
                    return result
        except asyncio.TimeoutError:
            self.close()
            raise
        except ConnectionError as e:
            self.close()
            logger.debug('connection error {}'.format(str(e)))
            raise ConnectionClosedError('the server has closed this connection') from e
        except asyncio.IncompleteReadError as e:
            self.close()
            raise ConnectionClosedError('Server connection has closed') from e

    @asyncio.coroutine
    def _recv(self, api):
        """
        A coroutine which receive response from the thrift server
        """
        fname, mtype, rseqid = yield from self._iprot.read_message_begin()
        if rseqid != self._seqid:
            # transport should be closed if bad seq happened
            self.close()
            raise TApplicationException(TApplicationException.BAD_SEQUENCE_ID,
                                        fname + ' failed: out of sequence response')

        if mtype == TMessageType.EXCEPTION:
            x = TApplicationException()
            yield from self._iprot.read_struct(x)
            yield from self._iprot.read_message_end()
            raise x
        result = getattr(self.service, api + '_result')()
        yield from self._iprot.read_struct(result)
        yield from self._iprot.read_message_end()

        if hasattr(result, "success") and result.success is not None:
            return result.success

        # void api without throws
        if len(result.thrift_spec) == 0:
            return

        # check throws
        for k, v in result.__dict__.items():
            if k != 'success' and v:
                raise v
        if hasattr(result, 'success'):
            raise TApplicationException(TApplicationException.MISSING_RESULT)

    def close(self):
        self._writer.close()
        self.closed = True
