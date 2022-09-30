__all__ = ["Socket"]


import socket
import io
import struct

import fastavro  # type: ignore

from ._schema import handshake_request, handshake_response
from ._logging import logger


BUFFSIZE = 4096


class Socket:
    def __init__(self, host, port):
        self._host = host
        self._port = port
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(None)
        self._socket.connect((host, port))
        self._named_types = {}

    def _read(self, response_schema):
        logger.info(
            f"{self._host}:{self._port} Socket._read response_schema {response_schema}"
        )
        logger.info(f"{self._host}:{self._port} Socket._read checkpoint A")
        response_schema = fastavro.parse_schema(
            response_schema, expand=True, named_schemas=self._named_types
        )
        # Needed twice for nested types, likely should be fixed upstream
        logger.info(f"{self._host}:{self._port} Socket._read checkpoint B")
        response_schema = fastavro.parse_schema(
            response_schema, expand=True, named_schemas=self._named_types
        )
        buf = io.BytesIO()
        remaining = 0
        logger.info(f"{self._host}:{self._port} Socket._read checkpoint C")
        while True:
            try:
                logger.info(f"{self._host}:{self._port} Socket._read try {buf.getvalue()[:64}}")
                buf.seek(0)
                obj = fastavro.schemaless_reader(buf, response_schema)
                return obj
            except Exception as e:
                logger.info(f"{self._host}:{self._port} Socket._read except {e}")
                buf.seek(0)
            if not remaining:
                logger.info(f"{self._host}:{self._port} Socket._read not remaining")
                remaining = struct.unpack_from(">L", self._socket.recv(4))[0]

            buf.seek(0, 2)
            logger.info(f"{self._host}:{self._port} Socket._read {remaining} remaining")
            num_read = buf.write(self._socket.recv(min(remaining, BUFFSIZE)))
            logger.info(f"{self._host}:{self._port} Socket._read {num_read} num_read")
            remaining -= num_read

    def _write(self, bytesio):
        bytesio.seek(0)
        out = bytesio.read()
        out = struct.pack(">L", len(out)) + out
        self._socket.sendall(out)

    def handshake(
        self, client_hash=b" " * 16, client_protocol=None, server_hash=b" " * 16
    ):
        # send request
        request = io.BytesIO()
        record = {
            "clientHash": client_hash,
            "clientProtocol": client_protocol,
            "serverHash": server_hash,
            "meta": {},
        }
        fastavro.schemaless_writer(request, handshake_request, record)
        self._write(request)
        self._write_metadata()
        self._write_method_name("")
        # read response
        response = self._read(handshake_response)
        self._read({"type": "map", "values": "bytes"})
        self._read("boolean")
        self._read("null")
        if response["match"] == "NONE":
            self.handshake(
                response["serverHash"],
                response["serverProtocol"],
                response["serverHash"],
            )
        return response["serverProtocol"]

    def message(self, method_name, method_schema, *args, **kwargs):
        self._validate_parameters(method_schema.get("request", []), *args, **kwargs)
        self._write_metadata()
        self._write_method_name(method_name)
        self._write_parameters(method_schema.get("request", []), *args, **kwargs)
        self._write_terminator()
        # read metadata
        _ = self._read({"type": "map", "values": "bytes"})
        # read error
        error = self._read("boolean")
        if error:
            raise Exception(self._read(["string"]))
        # read response
        response = self._read(method_schema.get("response", "null"))
        return response

    def _write_metadata(self, meta=None):
        if meta is None:
            meta = {}
        # write metadata
        out = io.BytesIO()
        fastavro.schemaless_writer(
            out, {"type": "map", "values": "bytes"}, meta
        )  # empty mapping
        self._write(out)

    def _write_method_name(self, method_name):
        # write method_name
        out = io.BytesIO()
        fastavro.schemaless_writer(out, "string", method_name)
        self._write(out)

    def _validate_parameters(self, method_request_schema, *args, **kwargs):
        # write parameters
        args = list(args)
        for parameter in method_request_schema:
            if parameter["name"] in kwargs:
                data = kwargs[parameter["name"]]
            elif args:
                data = args.pop(0)
            out = io.BytesIO()
            schema = fastavro.parse_schema(
                parameter["type"], expand=True, named_schemas=self._named_types
            )
            # Needed twice for nested types... Should likely be fixed upstream
            schema = fastavro.parse_schema(
                schema, expand=True, named_schemas=self._named_types
            )
            fastavro.schemaless_writer(out, schema, data)

    def _write_parameters(self, method_request_schema, *args, **kwargs):
        # write parameters
        args = list(args)
        for parameter in method_request_schema:
            if parameter["name"] in kwargs:
                data = kwargs[parameter["name"]]
            elif args:
                data = args.pop(0)
            out = io.BytesIO()
            schema = fastavro.parse_schema(
                parameter["type"], expand=True, named_schemas=self._named_types
            )
            # Needed twice for nested types... Should likely be fixed upstream
            schema = fastavro.parse_schema(
                schema, expand=True, named_schemas=self._named_types
            )
            fastavro.schemaless_writer(out, schema, data)
            self._write(out)

    def _write_terminator(self):
        # write terminate (zero length buffer)
        self._write(io.BytesIO())
