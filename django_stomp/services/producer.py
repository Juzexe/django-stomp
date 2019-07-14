import json
import logging
import ssl
import uuid
from contextlib import contextmanager
from typing import Callable
from typing import Dict

import tenacity
from django.core.serializers.json import DjangoJSONEncoder
from django_stomp.helpers import clean_dict_with_falsy_or_strange_values
from django_stomp.helpers import slow_down
from request_id_django_log.request_id import current_request_id
from request_id_django_log.settings import NO_REQUEST_ID
from stomp import Connection
from stomp.connect import StompConnection11

logger = logging.getLogger("django_stomp")


class Publisher:
    def __init__(self, connection: StompConnection11, connection_configuration: Dict) -> None:
        self._connection_configuration = connection_configuration
        self.connection = connection
        self._default_content_type = "application/json;charset=utf-8"

    def is_open(self):
        return self.connection.is_connected()

    @slow_down
    def start(self):
        self.connection.start()
        self.connection.connect(**self._connection_configuration)
        logger.info("Connected")

    def close(self):
        self.connection.disconnect()
        logger.info("Disconnected")

    def start_if_not_open(self):
        if not self.is_open():
            logger.info("It is not open. Starting...")
            self.start()

    def send(self, body: dict, queue: str, headers=None, persistent=True, attempt=10):
        correlation_id = current_request_id() if current_request_id() != NO_REQUEST_ID else uuid.uuid4()
        standard_header = {"correlation-id": correlation_id}
        if headers:
            standard_header.update(headers)
        if persistent:
            standard_header = self._add_persistent_messaging_header(standard_header)

        send_params = {
            "destination": queue,
            "body": json.dumps(body, cls=DjangoJSONEncoder),
            "headers": standard_header,
            "content_type": self._default_content_type,
            "transaction": getattr(self, "_tmp_transaction_id", None),
        }
        send_params = clean_dict_with_falsy_or_strange_values(send_params)

        def _internal_send_logic():
            self.start_if_not_open()
            self.connection.send(**send_params)

        self._retry_send(_internal_send_logic, attempt=attempt)

    @staticmethod
    def _retry_send(function: Callable, attempt=10, *args, **kwargs):
        retry_configuration = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(attempt),
            wait=tenacity.wait_fixed(3) + tenacity.wait_random(0, 2),
            after=tenacity.after_log(logger, logger.level) if logger else None,
            reraise=True,
        )
        return retry_configuration(function, *args, **kwargs)

    @staticmethod
    def _add_persistent_messaging_header(headers: Dict) -> Dict:
        value = {"persistent": "true"}

        if headers:
            headers.update(value)
            return headers

        return value


def build_publisher(**connection_params) -> Publisher:
    logger.info("Building publisher...")
    hosts = [(connection_params.get("host"), connection_params.get("port"))]
    use_ssl = connection_params.get("use_ssl", False)
    ssl_version = connection_params.get("ssl_version", ssl.PROTOCOL_TLS)
    logger.info(f"Use SSL? {use_ssl}. Version: {ssl_version}")
    client_id = connection_params.get("client_id", uuid.uuid4())
    connection_configuration = {
        "username": connection_params.get("username"),
        "passcode": connection_params.get("password"),
        "wait": True,
        "headers": {"client-id": f"{client_id}-publisher"},
    }
    conn = Connection(hosts, ssl_version=ssl_version, use_ssl=use_ssl)
    publisher = Publisher(conn, connection_configuration)
    return publisher


@contextmanager
def auto_open_close_connection(publisher: Publisher):
    try:
        publisher.start()
        yield
    finally:
        if publisher.is_open():
            publisher.close()


@contextmanager
def do_inside_transaction(publisher: Publisher):
    try:
        publisher.start_if_not_open()
        transaction_id = publisher.connection.begin()
        logger.debug("Created transaction ID: %s", transaction_id)
        setattr(publisher, "_tmp_transaction_id", transaction_id)
        yield
        publisher.connection.commit(transaction_id)
    except BaseException as e:
        logger.exception("Error inside transaction")
        if hasattr(publisher, "_tmp_transaction_id"):
            publisher.connection.abort(getattr(publisher, "_tmp_transaction_id"))
        raise e
    finally:
        if hasattr(publisher, "_tmp_transaction_id"):
            delattr(publisher, "_tmp_transaction_id")
