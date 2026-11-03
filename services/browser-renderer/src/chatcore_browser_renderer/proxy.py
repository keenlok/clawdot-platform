"""Compatibility re-exports for the shared core egress proxy."""

from chatcore.security.egress_proxy.proxy import MAX_HEADER_BYTES, EgressProxy, ProxyLease

__all__ = ["EgressProxy", "MAX_HEADER_BYTES", "ProxyLease"]
