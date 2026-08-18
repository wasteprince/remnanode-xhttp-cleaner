#!/usr/bin/env python3
"""Apply the XHTTP Cleaner patch to an exact Xray-core source tree.

The patch deliberately uses structural anchors instead of line numbers.  Every
anchor must match exactly once; otherwise the command stops without leaving a
partially patched tree.  This is the compatibility gate used for new Xray
versions.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path


PATCH_ID = "xhttp-cleaner-v5"


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one structural match, found {count}")
    return text.replace(old, new, 1)


def replace_one_variant(text: str, variants: tuple[tuple[str, str], ...], label: str) -> str:
    matches = [(old, new, text.count(old)) for old, new in variants if text.count(old)]
    total = sum(count for _old, _new, count in matches)
    if total != 1:
        raise PatchError(f"{label}: expected one supported structural match, found {total}")
    old, new, _count = matches[0]
    return text.replace(old, new, 1)


def patched_hub(source: str) -> str:
    source = replace_once(
        source,
        """	sessions       sync.Map
	localAddr      net.Addr
""",
        """	sessions       sync.Map
	reaperStop     *done.Instance
	localAddr      net.Addr
""",
        "request handler reaper lifecycle",
    )
    source = replace_once(
        source,
        """type httpSession struct {
	uploadQueue *uploadQueue
	// for as long as the GET request is not opened by the client, this will be
""",
        """type httpSession struct {
	uploadQueue *uploadQueue
	activity    *xhttpSessionActivity
	// for as long as the GET request is not opened by the client, this will be
""",
        "httpSession activity field",
    )
    source = replace_once(
        source,
        """	s := &httpSession{
		uploadQueue:      NewUploadQueue(h.ln.config.GetNormalizedScMaxBufferedPosts()),
		isFullyConnected: done.New(),
	}

	h.sessions.Store(sessionId, s)

	shouldReap := done.New()
	go func() {
		time.Sleep(30 * time.Second)
		shouldReap.Close()
	}()
	go func() {
		select {
		case <-shouldReap.Wait():
			h.sessions.Delete(sessionId)
			s.uploadQueue.Close()
		case <-s.isFullyConnected.Wait():
		}
	}()
""",
        """	s := &httpSession{
		uploadQueue:      NewUploadQueue(h.ln.config.GetNormalizedScMaxBufferedPosts()),
		activity:         newXHTTPSessionActivity(),
		isFullyConnected: done.New(),
	}
	s.uploadQueue.setActivityCallback(s.activity.touch)

	h.sessions.Store(sessionId, s)
	h.startXHTTPPreconnectExpiry(sessionId, s)
""",
        "session construction and upstream reaper",
    )
    downstream_conn = """		httpSC := &httpServerConn{
			Instance:       done.New(),
			Reader:         request.Body,
			ResponseWriter: writer,
		}
"""
    downstream_conn_with_activity = """		httpSC := &httpServerConn{
			Instance:       done.New(),
			Reader:         request.Body,
			ResponseWriter: writer,
			onActivity:     xhttpSessionTouch(currentSession),
		}
"""
    downstream_layouts = (
        # v26.6.27 through v26.7.11 used the listener address directly.
        """		conn := splitConn{
""",
        # v26.7.28 preserves the request's local address when one is present.
        """		localAddr := h.localAddr
		if la, ok := request.Context().Value(http.LocalAddrContextKey).(net.Addr); ok && la != nil {
			localAddr = la
		}
		conn := splitConn{
""",
    )
    source = replace_one_variant(
        source,
        tuple(
            (downstream_conn + layout, downstream_conn_with_activity + layout)
            for layout in downstream_layouts
        ),
        "downstream activity hook",
    )
    source = replace_once(
        source,
        """			currentSession.isFullyConnected.Close()
			defer h.sessions.Delete(sessionId)
""",
        """			currentSession.isFullyConnected.Close()
			defer h.sessions.CompareAndDelete(sessionId, currentSession)
""",
        "session completion reuse guard",
    )
    source = replace_once(
        source,
        """type httpServerConn struct {
	sync.Mutex
	*done.Instance
	io.Reader // no need to Close request.Body
	http.ResponseWriter
}
""",
        """type httpServerConn struct {
	sync.Mutex
	*done.Instance
	io.Reader // no need to Close request.Body
	http.ResponseWriter
	onActivity func()
}
""",
        "httpServerConn activity callback",
    )
    source = replace_once(
        source,
        """	n, err := c.ResponseWriter.Write(b)
	if err == nil {
		c.ResponseWriter.(http.Flusher).Flush()
	}
	return n, err
""",
        """	n, err := c.ResponseWriter.Write(b)
	if n > 0 && c.onActivity != nil {
		c.onActivity()
	}
	if err == nil {
		c.ResponseWriter.(http.Flusher).Flush()
	}
	return n, err
""",
        "downstream write accounting",
    )
    source = replace_once(
        source,
        """type Listener struct {
	sync.Mutex
	server     http.Server
""",
        """type Listener struct {
	sync.Mutex
	handler    *requestHandler
	server     http.Server
""",
        "listener owns reaper lifecycle",
    )
    source = replace_once(
        source,
        """		sessionMu:      &sync.Mutex{},
		sessions:       sync.Map{},
		socketSettings: streamSettings.SocketSettings,
	}
""",
        """		sessionMu:      &sync.Mutex{},
		sessions:       sync.Map{},
		reaperStop:     done.New(),
		socketSettings: streamSettings.SocketSettings,
	}
	l.handler = handler
""",
        "handler lifecycle construction",
    )
    source = replace_once(
        source,
        """
	return l, err
}

// Addr implements net.Listener.Addr().
""",
        """
	handler.startXHTTPReaper()
	return l, err
}

// Addr implements net.Listener.Addr().
""",
        "start listener reaper after successful setup",
    )
    source = replace_once(
        source,
        """\t\tl.server = http.Server{
\t\t\tHandler:           handler,
\t\t\tReadHeaderTimeout: time.Second * 4,
\t\t\tMaxHeaderBytes:    l.config.GetNormalizedServerMaxHeaderBytes(),
""",
        """\t\tl.server = http.Server{
\t\t\tHandler:           handler,
\t\t\tReadHeaderTimeout: time.Second * 4,
\t\t\tIdleTimeout:       xhttpCleanerHTTPIdleTimeout,
\t\t\tMaxHeaderBytes:    l.config.GetNormalizedServerMaxHeaderBytes(),
""",
        "XHTTP idle HTTP connection timeout",
    )
    source = replace_once(
        source,
        """func (ln *Listener) Close() error {
	if ln.h3server != nil {
""",
        """func (ln *Listener) Close() error {
	if ln.handler != nil {
		ln.handler.stopXHTTPReaper()
	}
	if ln.h3server != nil {
""",
        "stop listener reaper",
    )
    return source


def patched_upload_queue(source: str) -> str:
    source = replace_once(
        source,
        """	maxPackets    int
	closed        *done.Instance
}
""",
        """	maxPackets    int
	closed        *done.Instance
	onActivity    func()
}
""",
        "upload queue activity callback",
    )
    source = replace_once(
        source,
        """func (h *uploadQueue) Push(p Packet) error {
	if h.reader.Load() != nil || (p.Reader != nil && !h.reader.CompareAndSwap(nil, p.Reader)) {
""",
        """func (h *uploadQueue) Push(p Packet) error {
	if len(p.Payload) > 0 {
		h.touchActivity()
	}
	if h.reader.Load() != nil || (p.Reader != nil && !h.reader.CompareAndSwap(nil, p.Reader)) {
""",
        "queued payload activity accounting",
    )
    source = replace_once(
        source,
        """	if reader := h.reader.Load(); reader != nil {
		return reader.Read(b)
	}
""",
        """	if reader := h.reader.Load(); reader != nil {
		n, err := reader.Read(b)
		if n > 0 {
			h.touchActivity()
		}
		return n, err
	}
""",
        "stream upload read accounting",
    )
    source = replace_once(
        source,
        """		case p := <-h.pushedPackets:
			if p.Reader != nil {
				return p.Reader.Read(b)
			}
""",
        """		case p := <-h.pushedPackets:
			if p.Reader != nil {
				n, err := p.Reader.Read(b)
				if n > 0 {
					h.touchActivity()
				}
				return n, err
			}
""",
        "first stream upload read accounting",
    )
    source = replace_once(
        source,
        """			return n, nil
		}

		// misordered packet
""",
        """			if n > 0 {
				h.touchActivity()
			}
			return n, nil
		}

		// misordered packet
""",
        "packet upload read accounting",
    )
    return source


def patched_default_policy(source: str) -> str:
    variants = (
        (
            """\t\tdefaultBufferSize = 512 * 1024
""",
            """\t\t// A 512 KiB queue per direction retains several GiB on busy
\t\t// servers with thousands of sessions. 128 KiB preserves batching and
\t\t// backpressure while bounding XHTTP, raw TCP and gRPC pipe memory.
\t\tdefaultBufferSize = 128 * 1024
""",
        ),
        (
            """\t\t\treturn 512 * 1024
""",
            """\t\t\t// Bound the default XHTTP, raw TCP and gRPC pipe memory
\t\t\t// while preserving Xray's reloadable atomic policy in v26.7+.
\t\t\treturn 128 * 1024
""",
        ),
    )
    matches = [(old, new, source.count(old)) for old, new in variants if source.count(old)]
    total = sum(count for _old, _new, count in matches)
    if total != 1:
        raise PatchError(
            "default per-connection transport pipe budget: "
            f"expected one supported structural match, found {total}"
        )
    old, new, _count = matches[0]
    return source.replace(old, new, 1)


def patched_hysteria_hub(source: str) -> str:
    return replace_once(
        source,
        """\t\t\t\t\tudpIdleTimeout: time.Duration(h.config.UdpIdleTimeout) * time.Second,
""",
        """\t\t\t\t\t// Bound only inactive logical UDP sessions. The guard preserves
\t\t\t\t\t// upstream's 60s default and never changes active QUIC traffic.
\t\t\t\t\tudpIdleTimeout: boundedHysteriaUDPIdleTimeout(time.Duration(h.config.UdpIdleTimeout) * time.Second),
""",
        "Hysteria inactive UDP lifetime",
    )


def patched_hysteria_conn(source: str) -> str:
    source = replace_once(
        source,
        """\t\tfor _, udpConn := range m.m {
\t\t\tif now.Sub(udpConn.Time()) > m.udpIdleTimeout {
\t\t\t\ttimeoutConn = append(timeoutConn, udpConn)
\t\t\t}
\t\t}
""",
        """\t\tfor _, udpConn := range m.m {
\t\t\tif hysteriaUDPSessionExpired(udpConn, now, m.udpIdleTimeout) {
\t\t\t\ttimeoutConn = append(timeoutConn, udpConn)
\t\t\t}
\t\t}
""",
        "Hysteria UDP expiry scan",
    )
    source = replace_once(
        source,
        """\t\tfor _, udpConn := range timeoutConn {
\t\t\tm.Lock()
\t\t\tm.close(udpConn)
\t\t\tm.Unlock()
\t\t}
""",
        """\t\tfor _, udpConn := range timeoutConn {
\t\t\tm.Lock()
\t\t\t// Activity can arrive after the read-locked scan. Recheck both
\t\t\t// identity and last activity under the manager write lock so an
\t\t\t// active or recreated session is never closed as stale.
\t\t\tcurrent, exists := m.m[udpConn.id]
\t\t\tif exists && current == udpConn && !udpConn.closed &&
\t\t\t\thysteriaUDPSessionExpired(udpConn, time.Now(), m.udpIdleTimeout) {
\t\t\t\tm.close(udpConn)
\t\t\t}
\t\t\tm.Unlock()
\t\t}
""",
        "Hysteria UDP expiry activity recheck",
    )
    source = replace_once(
        source,
        """\tudpConn, ok := m.m[id]
\tif ok {
\t\tselect {
""",
        """\tudpConn, ok := m.m[id]
\tif ok {
\t\t// Count arrival immediately. Waiting for the dispatcher to drain the
\t\t// channel could otherwise make a busy session look inactive.
\t\tudpConn.Update()
\t\tselect {
""",
        "Hysteria inbound datagram activity accounting",
    )
    return source


def patched_main(source: str) -> str:
    return replace_once(
        source,
        """func main() {
\tos.Args = getArgsV4Compatible()

\tbase.RootCommand.Long = "Xray is a platform for building proxies."
""",
        """func main() {
\tos.Args = getArgsV4Compatible()
\t// Short-lived commands such as `xray version` must not start the memory
\t// optimizer or overwrite the running server's shared status file.
\tstartMemoryOptimizerForCommand(os.Args)

\tbase.RootCommand.Long = "Xray is a platform for building proxies."
""",
        "long-lived Xray command memory optimizer startup",
    )


def patch_tree(root: Path, assets: Path) -> None:
    hub = root / "transport/internet/splithttp/hub.go"
    queue = root / "transport/internet/splithttp/upload_queue.go"
    default_policy = root / "features/policy/policy.go"
    hysteria_hub = root / "transport/internet/hysteria/hub.go"
    hysteria_conn = root / "transport/internet/hysteria/conn.go"
    main = root / "main/main.go"
    destinations = {
        assets / "xhttp_cleaner_reaper.go": root / "transport/internet/splithttp/xhttp_cleaner_reaper.go",
        assets / "xhttp_cleaner_reaper_test.go": root / "transport/internet/splithttp/xhttp_cleaner_reaper_test.go",
        assets / "core_memory_optimizer.go": root / "main/xhttp_cleaner_memory_optimizer.go",
        assets / "core_memory_optimizer_test.go": root / "main/xhttp_cleaner_memory_optimizer_test.go",
        assets / "hysteria_memory_guard.go": root / "transport/internet/hysteria/xhttp_cleaner_memory_guard.go",
        assets / "hysteria_memory_guard_test.go": root / "transport/internet/hysteria/xhttp_cleaner_memory_guard_test.go",
    }
    source_paths = (hub, queue, default_policy, hysteria_hub, hysteria_conn, main)
    for path in source_paths:
        if not path.is_file():
            raise PatchError(f"required Xray source file is missing: {path}")
    if any(path.exists() for path in destinations.values()):
        raise PatchError("tree already contains XHTTP Cleaner overlay")

    originals = {
        path: path.read_text(encoding="utf-8")
        for path in source_paths
    }
    changes = {
        hub: patched_hub(originals[hub]),
        queue: patched_upload_queue(originals[queue]),
        default_policy: patched_default_policy(originals[default_policy]),
        hysteria_hub: patched_hysteria_hub(originals[hysteria_hub]),
        hysteria_conn: patched_hysteria_conn(originals[hysteria_conn]),
        main: patched_main(originals[main]),
    }

    for path in destinations:
        if not path.is_file():
            raise PatchError(f"patch asset is missing: {path}")

    # All transformations are calculated before the first write.  The caller
    # works in a disposable clone, but temp+replace also prevents torn files.
    for path, content in changes.items():
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
    for source, destination in destinations.items():
        shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the version-gated XHTTP Cleaner patch")
    parser.add_argument("source", type=Path, help="Xray-core source root")
    parser.add_argument("--assets", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        patch_tree(args.source.resolve(), args.assets.resolve())
    except (OSError, PatchError) as exc:
        print(f"patch_xray: incompatible source: {exc}", file=sys.stderr)
        return 2
    print(f"Applied {PATCH_ID} to {args.source.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
