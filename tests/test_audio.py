import httpx

from app.audio import extract_media_url, resolve_audio_url

PAGE = '''<html><body><audio id="myAudio" controls><source src="https://crm.test/api/filedownload/1?sp=r&amp;sig=abc%3D&amp;rsct=audio%2Fmpeg" type="audio/mpeg"></audio></body></html>'''


def test_extract():
    assert extract_media_url(PAGE) == "https://crm.test/api/filedownload/1?sp=r&sig=abc%3D&rsct=audio%2Fmpeg"
    assert extract_media_url("<html></html>") is None


def test_resolve_page_and_direct_audio():
    def handler(req):
        if req.url.path == "/page":
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_audio_url("https://org.test/page", client=c).startswith("https://crm.test/api/filedownload/1")
    assert resolve_audio_url("https://org.test/direct.mp3", client=c) == "https://org.test/direct.mp3"
