"""KataGo 适配器平台实现。"""
from .windows_native import WindowsNativeAdapter
from .ssh_remote import SshRemoteAdapter
from .http_remote import HttpRemoteAdapter

__all__ = ["WindowsNativeAdapter", "SshRemoteAdapter", "HttpRemoteAdapter"]
