"""
Durable queue infrastructure (Team Member 3, Phase 3): a broker-backed ``QueueProducer`` / ``QueueConsumer`` whose job ownership,
attempts and fencing live in the MongoDB lease store, and a worker runtime that keeps a lease alive while a job runs.

Importing this package does not import the AMQP client (``aio-pika``, extra ``queue``): it is imported when a connection is made.
"""

from doc_extractor.infrastructure.queue.errors import (
    BrokerConfigurationError,
    BrokerPublishError,
    BrokerUnavailableError,
    QueueError,
    QueueUsageError,
    ReceiptHandleError,
)
from doc_extractor.infrastructure.queue.rabbitmq import RabbitMQQueue, encode_message, job_key_of
from doc_extractor.infrastructure.queue.settings import BrokerSettings
from doc_extractor.infrastructure.queue.transport import AioPikaTransport, BrokerTransport, QueueTopology, build_topology

__all__ = [
    "AioPikaTransport",
    "BrokerConfigurationError",
    "BrokerPublishError",
    "BrokerSettings",
    "BrokerTransport",
    "BrokerUnavailableError",
    "QueueError",
    "QueueTopology",
    "QueueUsageError",
    "RabbitMQQueue",
    "ReceiptHandleError",
    "build_topology",
    "encode_message",
    "job_key_of",
]
