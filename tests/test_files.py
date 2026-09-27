"""Uploading an attachment and sharing it into a conversation."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from conftest import BACKEND, ROOM

from sable.files import SHARES_API, FilesClient, FilesError, safe_filename

USER = "sable-bot"
PASSWORD = "app-password"
DAV = f"{BACKEND}/remote.php/dav/files/{USER}"


def client(**overrides) -> FilesClient:
    return FilesClient(BACKEND, USER, PASSWORD, **overrides)


def ocs(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"ocs": {"meta": {"status": "ok"}, "data": data}})


def share_form(route) -> dict[str, str]:
    body = parse_qs(route.calls.last.request.content.decode())
    return {k: v[0] for k, v in body.items()}


# --------------------------------------------------------------------------- #
# Filenames
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given, must_contain",
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\evil.exe", "evil.exe"),
        ("subdir/nested/chart.png", "chart.png"),
        ("../../../", "attachment"),
        ("", "attachment"),
        ("spaces and (parens).txt", "spaces-and-parens-.txt"),
    ],
)
def test_filenames_are_sanitised(given: str, must_contain: str) -> None:
    result = safe_filename(given)
    assert must_contain in result
    # Nothing that could climb out of the upload folder.
    assert "/" not in result and "\\" not in result and ".." not in result


def test_filenames_are_made_unique() -> None:
    # Two alerts with the same filename must not overwrite each other.
    assert safe_filename("report.pdf") != safe_filename("report.pdf")


def test_a_very_long_filename_is_bounded_but_keeps_its_extension() -> None:
    result = safe_filename("x" * 400 + ".pdf")
    assert len(result) < 160
    assert result.endswith(".pdf")


# --------------------------------------------------------------------------- #
# Upload and share
# --------------------------------------------------------------------------- #


@respx.mock(assert_all_called=False)
async def test_send_file_uploads_then_shares(respx_mock) -> None:
    mkcol = respx_mock.request("MKCOL", f"{DAV}/sable").mock(
        return_value=httpx.Response(405)  # already exists
    )
    put = respx_mock.put(url__startswith=f"{DAV}/sable/").mock(
        return_value=httpx.Response(201)
    )
    share = respx_mock.post(f"{BACKEND}{SHARES_API}").mock(return_value=ocs({"id": 77}))

    files = client()
    try:
        result = await files.send_file(
            ROOM, "report.pdf", b"%PDF-1.7 data", caption="nightly build"
        )
    finally:
        await files.aclose()

    assert mkcol.called and put.called and share.called
    assert result.share_id == 77
    assert result.size == len(b"%PDF-1.7 data")
    assert result.name.endswith("-report.pdf")
    assert result.path.startswith("/sable/")

    # The upload carried the bytes, authenticated as the user.
    assert put.calls.last.request.content == b"%PDF-1.7 data"
    assert "authorization" in put.calls.last.request.headers

    form = share_form(share)
    assert form["shareType"] == "10"
    assert form["shareWith"] == ROOM
    assert form["path"] == result.path
    assert json.loads(form["talkMetaData"]) == {
        "messageType": "comment",
        "caption": "nightly build",
    }
    assert share.calls.last.request.headers["ocs-apirequest"] == "true"


@respx.mock(assert_all_called=False)
async def test_silent_and_reply_to_ride_along_in_the_metadata(respx_mock) -> None:
    respx_mock.request("MKCOL", f"{DAV}/sable").mock(return_value=httpx.Response(405))
    respx_mock.put(url__startswith=f"{DAV}/sable/").mock(return_value=httpx.Response(201))
    share = respx_mock.post(f"{BACKEND}{SHARES_API}").mock(return_value=ocs({"id": 1}))

    files = client()
    try:
        await files.send_file(ROOM, "a.txt", b"x", silent=True, reply_to=42)
    finally:
        await files.aclose()

    meta = json.loads(share_form(share)["talkMetaData"])
    assert meta["silent"] is True
    assert meta["replyTo"] == 42


@respx.mock(assert_all_called=False)
async def test_the_folder_is_created_when_missing(respx_mock, caplog) -> None:
    mkcol = respx_mock.request("MKCOL", f"{DAV}/sable").mock(
        return_value=httpx.Response(201)
    )
    respx_mock.put(url__startswith=f"{DAV}/sable/").mock(return_value=httpx.Response(201))
    respx_mock.post(f"{BACKEND}{SHARES_API}").mock(return_value=ocs({"id": 1}))

    files = client()
    try:
        with caplog.at_level(logging.INFO):
            await files.send_file(ROOM, "a.txt", b"x")
            await files.send_file(ROOM, "b.txt", b"y")
    finally:
        await files.aclose()

    # Created once, then remembered.
    assert len(mkcol.calls) == 1
    assert "created /sable" in caplog.text


@respx.mock(assert_all_called=False)
async def test_a_custom_upload_folder_is_used(respx_mock) -> None:
    mkcol = respx_mock.request("MKCOL", f"{DAV}/alerts/incoming").mock(
        return_value=httpx.Response(405)
    )
    put = respx_mock.put(url__startswith=f"{DAV}/alerts/incoming/").mock(
        return_value=httpx.Response(201)
    )
    respx_mock.post(f"{BACKEND}{SHARES_API}").mock(return_value=ocs({"id": 1}))

    files = client(upload_path="alerts/incoming")
    try:
        result = await files.send_file(ROOM, "a.txt", b"x")
    finally:
        await files.aclose()
    assert mkcol.called and put.called
    assert result.path.startswith("/alerts/incoming/")


@respx.mock(assert_all_called=False)
async def test_a_failed_share_cleans_up_the_orphaned_upload(respx_mock, caplog) -> None:
    respx_mock.request("MKCOL", f"{DAV}/sable").mock(return_value=httpx.Response(405))
    respx_mock.put(url__startswith=f"{DAV}/sable/").mock(return_value=httpx.Response(201))
    respx_mock.post(f"{BACKEND}{SHARES_API}").mock(
        return_value=httpx.Response(404, text="conversation not found")
    )
    delete = respx_mock.delete(url__startswith=f"{DAV}/sable/").mock(
        return_value=httpx.Response(204)
    )

    files = client()
    try:
        with caplog.at_level(logging.INFO):
            with pytest.raises(FilesError) as excinfo:
                await files.send_file(ROOM, "report.pdf", b"data")
    finally:
        await files.aclose()

    # Otherwise the file sits in the bot user's Files, shared with nobody.
    assert delete.called
    assert excinfo.value.status == 404
    assert "cleaned up" in caplog.text


@respx.mock(assert_all_called=False)
async def test_a_failed_upload_is_reported_with_its_status(respx_mock) -> None:
    respx_mock.request("MKCOL", f"{DAV}/sable").mock(return_value=httpx.Response(405))
    respx_mock.put(url__startswith=f"{DAV}/sable/").mock(
        return_value=httpx.Response(507, text="insufficient storage")
    )
    files = client()
    try:
        with pytest.raises(FilesError) as excinfo:
            await files.send_file(ROOM, "big.bin", b"x")
    finally:
        await files.aclose()
    assert excinfo.value.status == 507
    assert "insufficient storage" in str(excinfo.value)


@respx.mock(assert_all_called=False)
async def test_an_unreachable_nextcloud_is_a_files_error(respx_mock) -> None:
    respx_mock.request("MKCOL", f"{DAV}/sable").mock(
        side_effect=httpx.ConnectError("refused")
    )
    files = client()
    try:
        with pytest.raises(FilesError, match="could not reach"):
            await files.send_file(ROOM, "a.txt", b"x")
    finally:
        await files.aclose()
