"""A stand-in for boto3, so the tests run without AWS or credentials.

It is installed before any test module imports the code under test, so a client
built at import time is a fake one too. It answers the usual ways of writing an
object (a client's put_object, upload_fileobj and upload_file, a resource's
Object(...).put and Bucket(...).put_object, and the same through a Session), and
records whatever lands in `stored_objects`.
"""
from __future__ import annotations

import sys
import types

import pytest

stored_objects = {}


def _put(bucket, key, body):
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, str):
        body = body.encode("utf-8")
    stored_objects[(bucket, key)] = bytes(body)


class _Client:
    def put_object(self, *, Bucket, Key, Body=b"", **_):
        _put(Bucket, Key, Body)
        return {"ETag": '"acme"'}

    def upload_fileobj(self, Fileobj, Bucket, Key, **_):
        _put(Bucket, Key, Fileobj)

    def upload_file(self, Filename, Bucket, Key, **_):
        with open(Filename, "rb") as handle:
            _put(Bucket, Key, handle.read())


class _Object:
    def __init__(self, bucket, key):
        self.bucket_name, self.key = bucket, key

    def put(self, Body=b"", **_):
        _put(self.bucket_name, self.key, Body)
        return {"ETag": '"acme"'}


class _Bucket:
    def __init__(self, name):
        self.name = name

    def put_object(self, *, Key, Body=b"", **_):
        _put(self.name, Key, Body)
        return _Object(self.name, Key)

    def upload_fileobj(self, Fileobj, Key, **_):
        _put(self.name, Key, Fileobj)


class _Resource:
    def __init__(self):
        self.meta = types.SimpleNamespace(client=_Client())

    def Object(self, bucket, key):
        return _Object(bucket, key)

    def Bucket(self, name):
        return _Bucket(name)


def _only_s3(service_name):
    if service_name != "s3":
        raise AssertionError(f"only S3 is faked in these tests, not {service_name}")


def _client(service_name, *args, **kwargs):
    _only_s3(service_name)
    return _Client()


def _resource(service_name, *args, **kwargs):
    _only_s3(service_name)
    return _Resource()


class _Session:
    def __init__(self, *args, **kwargs):
        pass

    def client(self, service_name, *args, **kwargs):
        return _client(service_name)

    def resource(self, service_name, *args, **kwargs):
        return _resource(service_name)


class _ClientError(Exception):
    def __init__(self, error_response=None, operation_name=""):
        super().__init__(str(error_response))
        self.response = error_response or {}
        self.operation_name = operation_name


boto3 = types.ModuleType("boto3")
boto3.client, boto3.resource, boto3.Session = _client, _resource, _Session
boto3_session = types.ModuleType("boto3.session")
boto3_session.Session = _Session
boto3.session = boto3_session
botocore = types.ModuleType("botocore")
botocore_exceptions = types.ModuleType("botocore.exceptions")
botocore_exceptions.ClientError = _ClientError
botocore_exceptions.BotoCoreError = type("BotoCoreError", (Exception,), {})
botocore_exceptions.NoCredentialsError = type("NoCredentialsError", (botocore_exceptions.BotoCoreError,), {})
botocore.exceptions = botocore_exceptions
sys.modules.update(
    {
        "boto3": boto3,
        "boto3.session": boto3_session,
        "botocore": botocore,
        "botocore.exceptions": botocore_exceptions,
    }
)


@pytest.fixture(autouse=True)
def s3():
    stored_objects.clear()
    yield stored_objects
    stored_objects.clear()
