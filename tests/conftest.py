"""Fixtures of the server tests. They import the server only when used, so that
test collection order stays what it was: server_stubs swaps in a fake flask."""

import pytest


@pytest.fixture
def store(tmp_path):
    from pipeline_fakes import store_env

    yield from store_env(tmp_path)


@pytest.fixture
def pipeline(tmp_path):
    from pipeline_fakes import pipeline_env

    yield from pipeline_env(tmp_path)
