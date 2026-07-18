package splithttp

import (
	"testing"
	"time"

	"github.com/xtls/xray-core/common/signal/done"
)

func testXHTTPSession(lastActivity time.Time) *httpSession {
	session := &httpSession{
		uploadQueue:      NewUploadQueue(4),
		activity:         newXHTTPSessionActivity(),
		isFullyConnected: done.New(),
	}
	session.activity.lastUnixNano.Store(lastActivity.UnixNano())
	return session
}

func TestXHTTPCleanerKeepsRecentlyActiveSession(t *testing.T) {
	now := time.Now()
	handler := &requestHandler{}
	session := testXHTTPSession(now.Add(-xhttpCleanerIdleTTL + time.Second))
	handler.sessions.Store("session", session)
	if handler.reapXHTTPSessionIfIdle("session", session, now) {
		t.Fatal("active session was reaped")
	}
	if got, ok := handler.sessions.Load("session"); !ok || got != session {
		t.Fatal("active session disappeared")
	}
}

func TestXHTTPCleanerReapsExactIdleSession(t *testing.T) {
	now := time.Now()
	handler := &requestHandler{}
	session := testXHTTPSession(now.Add(-xhttpCleanerIdleTTL - time.Second))
	handler.sessions.Store("session", session)
	if !handler.reapXHTTPSessionIfIdle("session", session, now) {
		t.Fatal("idle session was not reaped")
	}
	if _, ok := handler.sessions.Load("session"); ok {
		t.Fatal("idle session is still registered")
	}
	if !session.uploadQueue.closed.Done() {
		t.Fatal("idle session queue was not closed")
	}
}

func TestXHTTPCleanerDoesNotDeleteReusedSessionID(t *testing.T) {
	now := time.Now()
	handler := &requestHandler{}
	oldSession := testXHTTPSession(now.Add(-xhttpCleanerIdleTTL - time.Second))
	newSession := testXHTTPSession(now)
	handler.sessions.Store("same-id", newSession)
	if handler.reapXHTTPSessionIfIdle("same-id", oldSession, now) {
		t.Fatal("stale reaper claimed a replacement session")
	}
	if got, ok := handler.sessions.Load("same-id"); !ok || got != newSession {
		t.Fatal("replacement session was deleted")
	}
}

func TestXHTTPCleanerPayloadTouchRefreshesActivity(t *testing.T) {
	session := testXHTTPSession(time.Now().Add(-time.Hour))
	session.uploadQueue.setActivityCallback(session.activity.touch)
	before := session.activity.lastUnixNano.Load()
	if err := session.uploadQueue.Push(Packet{Payload: []byte("payload")}); err != nil {
		t.Fatal(err)
	}
	if session.activity.lastUnixNano.Load() <= before {
		t.Fatal("payload did not refresh session activity")
	}
}

func TestXHTTPCleanerStopClosesAllRegisteredSessions(t *testing.T) {
	handler := &requestHandler{reaperStop: done.New()}
	first := testXHTTPSession(time.Now())
	second := testXHTTPSession(time.Now())
	handler.sessions.Store("first", first)
	handler.sessions.Store("second", second)
	handler.stopXHTTPReaper()
	if !handler.reaperStop.Done() || !first.uploadQueue.closed.Done() || !second.uploadQueue.closed.Done() {
		t.Fatal("listener shutdown did not close every registered session")
	}
	if _, ok := handler.sessions.Load("first"); ok {
		t.Fatal("first session remained registered")
	}
	if _, ok := handler.sessions.Load("second"); ok {
		t.Fatal("second session remained registered")
	}
}
