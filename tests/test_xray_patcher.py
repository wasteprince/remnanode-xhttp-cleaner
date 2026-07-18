import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("patch_xray", ROOT / "xray_patch/patch_xray.py")
assert SPEC and SPEC.loader
patch_xray = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patch_xray)


HUB_FIXTURE = """package splithttp
\tsessions       sync.Map
\tlocalAddr      net.Addr
type httpSession struct {
\tuploadQueue *uploadQueue
\t// for as long as the GET request is not opened by the client, this will be
}
\ts := &httpSession{
\t\tuploadQueue:      NewUploadQueue(h.ln.config.GetNormalizedScMaxBufferedPosts()),
\t\tisFullyConnected: done.New(),
\t}

\th.sessions.Store(sessionId, s)

\tshouldReap := done.New()
\tgo func() {
\t\ttime.Sleep(30 * time.Second)
\t\tshouldReap.Close()
\t}()
\tgo func() {
\t\tselect {
\t\tcase <-shouldReap.Wait():
\t\t\th.sessions.Delete(sessionId)
\t\t\ts.uploadQueue.Close()
\t\tcase <-s.isFullyConnected.Wait():
\t\t}
\t}()
\t\thttpSC := &httpServerConn{
\t\t\tInstance:       done.New(),
\t\t\tReader:         request.Body,
\t\t\tResponseWriter: writer,
\t\t}
\t\tconn := splitConn{
\t\t\tcurrentSession.isFullyConnected.Close()
\t\t\tdefer h.sessions.Delete(sessionId)
type httpServerConn struct {
\tsync.Mutex
\t*done.Instance
\tio.Reader // no need to Close request.Body
\thttp.ResponseWriter
}
\tn, err := c.ResponseWriter.Write(b)
\tif err == nil {
\t\tc.ResponseWriter.(http.Flusher).Flush()
\t}
\treturn n, err
type Listener struct {
\tsync.Mutex
\tserver     http.Server
\t\tsessionMu:      &sync.Mutex{},
\t\tsessions:       sync.Map{},
\t\tsocketSettings: streamSettings.SocketSettings,
\t}

\treturn l, err
}

// Addr implements net.Listener.Addr().
func (ln *Listener) Close() error {
\tif ln.h3server != nil {
"""


QUEUE_FIXTURE = """package splithttp
\tmaxPackets    int
\tclosed        *done.Instance
}
func (h *uploadQueue) Push(p Packet) error {
\tif h.reader.Load() != nil || (p.Reader != nil && !h.reader.CompareAndSwap(nil, p.Reader)) {
\tif reader := h.reader.Load(); reader != nil {
\t\treturn reader.Read(b)
\t}
\t\tcase p := <-h.pushedPackets:
\t\t\tif p.Reader != nil {
\t\t\t\treturn p.Reader.Read(b)
\t\t\t}
\t\t\treturn n, nil
\t\t}

\t\t// misordered packet
"""


class PatcherTests(unittest.TestCase):
    def test_current_structural_contract_is_patched(self):
        hub = patch_xray.patched_hub(HUB_FIXTURE)
        queue = patch_xray.patched_upload_queue(QUEUE_FIXTURE)
        self.assertIn("activity    *xhttpSessionActivity", hub)
        self.assertIn("startXHTTPPreconnectExpiry", hub)
        self.assertIn("handler.startXHTTPReaper()", hub)
        self.assertIn("xhttpSessionTouch(currentSession)", hub)
        self.assertIn("CompareAndDelete(sessionId, currentSession)", hub)
        self.assertIn("touchActivity", queue)

    def test_changed_upstream_fails_closed(self):
        with self.assertRaises(patch_xray.PatchError):
            patch_xray.patched_hub("package splithttp\n")

    def test_tree_is_not_partially_written_on_anchor_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "transport/internet/splithttp"
            package.mkdir(parents=True)
            hub = package / "hub.go"
            queue = package / "upload_queue.go"
            hub.write_text(HUB_FIXTURE, encoding="utf-8")
            queue.write_text("incompatible", encoding="utf-8")
            before = hub.read_text(encoding="utf-8")
            with self.assertRaises(patch_xray.PatchError):
                patch_xray.patch_tree(root, ROOT / "xray_patch")
            self.assertEqual(hub.read_text(encoding="utf-8"), before)
            self.assertFalse((package / "xhttp_cleaner_reaper.go").exists())


if __name__ == "__main__":
    unittest.main()
