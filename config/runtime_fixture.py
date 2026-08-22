from __future__ import annotations


def valid_override(duration: int) -> str:
    """Return the canonical Order Service override used by reload probes."""
    return f"""dataConnectors:
  inventoryServiceApi:
    address: dns:///inventoryservice:9202
    connectionsCount: 1
  orderEvents:
    brokers: redpanda:9092
    password: ""
    saslMechanism: SCRAM-SHA-512
    securityProtocol: PLAINTEXT
    username: ""
pools:
  defaultPool:
    executorsCount: 2
services:
  orderService:
    defaultGrpcTimeout: 5000
    environment: debug
    grpcHost: 0.0.0.0
    grpcPort: 9201
    httpHost: 0.0.0.0
    httpPort: 9091
endpoints:
  orderProcessed:
    enabled: false
streams:
  softDeadline:
    duration: {duration}
"""
