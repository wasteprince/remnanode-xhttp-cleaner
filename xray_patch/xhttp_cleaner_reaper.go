package splithttp

// XHTTP Cleaner overlay.  This file is copied into the exact upstream Xray
// release selected by the host manager.  The small hooks in hub.go and
// upload_queue.go feed this reaper with real payload activity.

import (
	"sync/atomic"
	"time"
)

const (
	xhttpCleanerIdleTTL       = 5 * time.Minute
	xhttpCleanerCheckInterval = 5 * time.Minute
	xhttpCleanerPreconnectTTL = 30 * time.Second
	// net/http applies IdleTimeout only while waiting for the next request. A
	// long-running XHTTP stream remains active and is not interrupted by it.
	xhttpCleanerHTTPIdleTimeout = 5 * time.Minute
)

type xhttpSessionActivity struct {
	lastUnixNano atomic.Int64
}

func newXHTTPSessionActivity() *xhttpSessionActivity {
	a := &xhttpSessionActivity{}
	a.touch()
	return a
}

func (a *xhttpSessionActivity) touch() {
	if a != nil {
		a.lastUnixNano.Store(time.Now().UnixNano())
	}
}

func (a *xhttpSessionActivity) idleAt(now time.Time) time.Duration {
	if a == nil {
		return 0
	}
	return now.Sub(time.Unix(0, a.lastUnixNano.Load()))
}

func xhttpSessionTouch(session *httpSession) func() {
	if session == nil || session.activity == nil {
		return nil
	}
	return session.activity.touch
}

func (h *uploadQueue) setActivityCallback(callback func()) {
	h.onActivity = callback
}

func (h *uploadQueue) touchActivity() {
	if h.onActivity != nil {
		h.onActivity()
	}
}

// reapXHTTPSessionIfIdle removes only the exact session pointer examined by
// the reaper.  CompareAndDelete is the guard against a new session reusing the
// same public session ID (and, consequently, the same client IP).
func (h *requestHandler) reapXHTTPSessionIfIdle(sessionID string, session *httpSession, now time.Time) bool {
	if session == nil || session.activity.idleAt(now) < xhttpCleanerIdleTTL {
		return false
	}
	if !h.sessions.CompareAndDelete(sessionID, session) {
		return false
	}
	_ = session.uploadQueue.Close()
	return true
}

func (h *requestHandler) expireUnconnectedXHTTPSession(sessionID string, session *httpSession) bool {
	if session == nil || session.isFullyConnected.Done() {
		return false
	}
	if !h.sessions.CompareAndDelete(sessionID, session) {
		return false
	}
	_ = session.uploadQueue.Close()
	return true
}

func (h *requestHandler) startXHTTPPreconnectExpiry(sessionID string, session *httpSession) {
	go func() {
		timer := time.NewTimer(xhttpCleanerPreconnectTTL)
		defer timer.Stop()
		select {
		case <-timer.C:
			h.expireUnconnectedXHTTPSession(sessionID, session)
		case <-session.isFullyConnected.Wait():
		case <-session.uploadQueue.closed.Wait():
		case <-h.reaperStop.Wait():
		}
	}()
}

// A handler owns one long-lived reaper regardless of the number of sessions.
// This avoids retaining one goroutine per connected XHTTP client.
func (h *requestHandler) startXHTTPReaper() {
	go func() {
		ticker := time.NewTicker(xhttpCleanerCheckInterval)
		defer ticker.Stop()
		for {
			select {
			case now := <-ticker.C:
				h.sessions.Range(func(key, value any) bool {
					sessionID, idOK := key.(string)
					session, sessionOK := value.(*httpSession)
					if idOK && sessionOK {
						h.reapXHTTPSessionIfIdle(sessionID, session, now)
					}
					return true
				})
			case <-h.reaperStop.Wait():
				return
			}
		}
	}()
}

func (h *requestHandler) stopXHTTPReaper() {
	if h.reaperStop == nil || h.reaperStop.Done() {
		return
	}
	_ = h.reaperStop.Close()
	h.sessions.Range(func(key, value any) bool {
		sessionID, idOK := key.(string)
		session, sessionOK := value.(*httpSession)
		if idOK && sessionOK && h.sessions.CompareAndDelete(sessionID, session) {
			_ = session.uploadQueue.Close()
		}
		return true
	})
}
