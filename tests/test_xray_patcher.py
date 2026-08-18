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
\t\tl.server = http.Server{
\t\t\tHandler:           handler,
\t\t\tReadHeaderTimeout: time.Second * 4,
\t\t\tMaxHeaderBytes:    l.config.GetNormalizedServerMaxHeaderBytes(),

\treturn l, err
}

// Addr implements net.Listener.Addr().
func (ln *Listener) Close() error {
\tif ln.h3server != nil {
"""


HUB_FIXTURE_26_7_28 = HUB_FIXTURE.replace(
    """\t\thttpSC := &httpServerConn{
\t\t\tInstance:       done.New(),
\t\t\tReader:         request.Body,
\t\t\tResponseWriter: writer,
\t\t}
\t\tconn := splitConn{
""",
    """\t\thttpSC := &httpServerConn{
\t\t\tInstance:       done.New(),
\t\t\tReader:         request.Body,
\t\t\tResponseWriter: writer,
\t\t}
\t\t_ = currentSession.uploadQueue.Push(Packet{Reader: httpSC})
\t\thttpSC := &httpServerConn{
\t\t\tInstance:       done.New(),
\t\t\tReader:         request.Body,
\t\t\tResponseWriter: writer,
\t\t}
\t\tlocalAddr := h.localAddr
\t\tif la, ok := request.Context().Value(http.LocalAddrContextKey).(net.Addr); ok && la != nil {
\t\t\tlocalAddr = la
\t\t}
\t\tconn := splitConn{
""",
)


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


POLICY_FIXTURE = """package policy
\t\tdefaultBufferSize = 512 * 1024
"""


POLICY_FIXTURE_26_7 = """package policy
var defaultBufferSize atomic.Int32
func readDefaultBufferSize() int32 {
\t\tdefault:
\t\t\treturn 512 * 1024
}
"""


HYSTERIA_HUB_FIXTURE = """package hysteria
\t\t\t\t\tudpIdleTimeout: time.Duration(h.config.UdpIdleTimeout) * time.Second,
"""


HYSTERIA_CONN_FIXTURE = """package hysteria
\t\tfor _, udpConn := range m.m {
\t\t\tif now.Sub(udpConn.Time()) > m.udpIdleTimeout {
\t\t\t\ttimeoutConn = append(timeoutConn, udpConn)
\t\t\t}
\t\t}
\t\tfor _, udpConn := range timeoutConn {
\t\t\tm.Lock()
\t\t\tm.close(udpConn)
\t\t\tm.Unlock()
\t\t}
\tudpConn, ok := m.m[id]
\tif ok {
\t\tselect {
"""


MAIN_FIXTURE = """package main
func main() {
\tos.Args = getArgsV4Compatible()

\tbase.RootCommand.Long = "Xray is a platform for building proxies."
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
        self.assertIn("IdleTimeout:       xhttpCleanerHTTPIdleTimeout", hub)
        self.assertIn("touchActivity", queue)
        policy = patch_xray.patched_default_policy(POLICY_FIXTURE)
        self.assertIn("defaultBufferSize = 128 * 1024", policy)

    def test_reloadable_v26_7_policy_is_patched_without_removing_atomic_storage(self):
        policy = patch_xray.patched_default_policy(POLICY_FIXTURE_26_7)
        self.assertIn("var defaultBufferSize atomic.Int32", policy)
        self.assertIn("return 128 * 1024", policy)
        self.assertNotIn("return 512 * 1024", policy)

    def test_v26_7_28_downstream_layout_is_patched_without_touching_stream_up(self):
        hub = patch_xray.patched_hub(HUB_FIXTURE_26_7_28)
        stream_up, downstream = hub.split("_ = currentSession.uploadQueue.Push", 1)
        self.assertNotIn("xhttpSessionTouch(currentSession)", stream_up)
        self.assertIn("xhttpSessionTouch(currentSession)", downstream)
        self.assertIn("localAddr := h.localAddr", downstream)

    def test_hysteria_idle_sessions_are_bounded_and_activity_safe(self):
        hub = patch_xray.patched_hysteria_hub(HYSTERIA_HUB_FIXTURE)
        conn = patch_xray.patched_hysteria_conn(HYSTERIA_CONN_FIXTURE)
        self.assertIn("boundedHysteriaUDPIdleTimeout", hub)
        self.assertIn("hysteriaUDPSessionExpired", conn)
        self.assertIn("current == udpConn", conn)
        self.assertIn("udpConn.Update()", conn)

    def test_memory_optimizer_starts_only_after_command_normalization(self):
        main = patch_xray.patched_main(MAIN_FIXTURE)
        self.assertIn("startMemoryOptimizerForCommand(os.Args)", main)
        self.assertLess(
            main.index("os.Args = getArgsV4Compatible()"),
            main.index("startMemoryOptimizerForCommand(os.Args)"),
        )

    def test_multiple_supported_policy_anchors_fail_closed(self):
        with self.assertRaises(patch_xray.PatchError):
            patch_xray.patched_default_policy(POLICY_FIXTURE + POLICY_FIXTURE_26_7)

    def test_changed_upstream_fails_closed(self):
        with self.assertRaises(patch_xray.PatchError):
            patch_xray.patched_hub("package splithttp\n")

    def test_tree_is_not_partially_written_on_anchor_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "transport/internet/splithttp"
            package.mkdir(parents=True)
            policy_package = root / "features/policy"
            policy_package.mkdir(parents=True)
            hysteria_package = root / "transport/internet/hysteria"
            hysteria_package.mkdir(parents=True)
            main_package = root / "main"
            main_package.mkdir()
            hub = package / "hub.go"
            queue = package / "upload_queue.go"
            policy = policy_package / "policy.go"
            hysteria_hub = hysteria_package / "hub.go"
            hysteria_conn = hysteria_package / "conn.go"
            main = main_package / "main.go"
            hub.write_text(HUB_FIXTURE, encoding="utf-8")
            queue.write_text("incompatible", encoding="utf-8")
            policy.write_text(POLICY_FIXTURE, encoding="utf-8")
            hysteria_hub.write_text(HYSTERIA_HUB_FIXTURE, encoding="utf-8")
            hysteria_conn.write_text(HYSTERIA_CONN_FIXTURE, encoding="utf-8")
            main.write_text(MAIN_FIXTURE, encoding="utf-8")
            before = hub.read_text(encoding="utf-8")
            with self.assertRaises(patch_xray.PatchError):
                patch_xray.patch_tree(root, ROOT / "xray_patch")
            self.assertEqual(hub.read_text(encoding="utf-8"), before)
            self.assertFalse((package / "xhttp_cleaner_reaper.go").exists())


if __name__ == "__main__":
    unittest.main()
