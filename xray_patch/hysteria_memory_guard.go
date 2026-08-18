package hysteria

// This overlay bounds how long an inactive logical Hysteria UDP session keeps
// its dispatcher, outbound socket and transport pipes reachable. It does not
// close an active QUIC connection and it does not change QUIC flow-control or
// packet queue sizes. An expired logical UDP session is recreated by the
// existing server feed path if the client sends its session ID again.

import (
	"os"
	"strings"
	"sync/atomic"
	"time"
)

const (
	hysteriaCleanerMinimumUDPIdleCap = 30 * time.Second
	hysteriaCleanerMaximumUDPIdleCap = 10 * time.Minute
	hysteriaCleanerActivityGrace     = 30 * time.Second
)

// CleanerMemoryRelease describes Go objects which became reclaimable after a
// logical UDP session was detached from the Hysteria session manager.
type CleanerMemoryRelease struct {
	Sessions           uint64
	QueuedBytes        uint64
	IdleTimeoutSeconds uint64
}

type CleanerMemoryReleaseHook func(CleanerMemoryRelease)

var hysteriaCleanerMemoryReleaseHook atomic.Value

// SetCleanerMemoryReleaseHook installs the process-wide, non-blocking observer
// used by the main memory optimizer. Xray has one optimizer and installs this
// hook before the server starts accepting traffic.
func SetCleanerMemoryReleaseHook(hook CleanerMemoryReleaseHook) {
	if hook != nil {
		hysteriaCleanerMemoryReleaseHook.Store(hook)
	}
}

func notifyHysteriaCleanerMemoryRelease(sessions, queuedBytes uint64, idleTimeout time.Duration) {
	value := hysteriaCleanerMemoryReleaseHook.Load()
	if value == nil {
		return
	}
	value.(CleanerMemoryReleaseHook)(CleanerMemoryRelease{
		Sessions:           sessions,
		QueuedBytes:        queuedBytes,
		IdleTimeoutSeconds: uint64(idleTimeout / time.Second),
	})
}

func notifyHysteriaCleanerConfiguration(idleTimeout time.Duration) {
	notifyHysteriaCleanerMemoryRelease(0, 0, idleTimeout)
}

// releaseHysteriaUDPQueue clears payload references from a channel which has
// already been closed while the manager lock prevents any new sender. Receive
// operations zero the channel slots, allowing the byte slices to be collected
// without waiting for a stalled dispatcher to drain old datagrams.
func releaseHysteriaUDPQueue(ch <-chan []byte) uint64 {
	var released uint64
	for payload := range ch {
		released += uint64(cap(payload))
	}
	return released
}

func hysteriaCleanerUDPIdleCap() time.Duration {
	raw := strings.TrimSpace(os.Getenv("XRAY_HYSTERIA_UDP_IDLE_CAP"))
	if raw == "" {
		return 0
	}
	switch strings.ToLower(raw) {
	case "0", "false", "off", "no":
		return 0
	}
	value, err := time.ParseDuration(raw)
	if err != nil || value < hysteriaCleanerMinimumUDPIdleCap || value > hysteriaCleanerMaximumUDPIdleCap {
		return 0
	}
	return value
}

func configuredHysteriaUDPIdleTimeout(configured time.Duration) time.Duration {
	// Upstream uses 60 seconds when the JSON field is omitted. Preserve that
	// behavior for defensive callers which pass an empty protobuf value.
	if configured <= 0 {
		configured = time.Minute
	}
	cap := hysteriaCleanerUDPIdleCap()
	if cap > 0 && configured > cap {
		configured = cap
	}
	notifyHysteriaCleanerConfiguration(configured)
	return configured
}

func configuredHysteriaUDPIdleTimeoutSeconds(seconds int64) time.Duration {
	if seconds <= 0 {
		return configuredHysteriaUDPIdleTimeout(0)
	}
	maximum := time.Duration(1<<63-1) - hysteriaCleanerActivityGrace
	maximumSeconds := int64(maximum / time.Second)
	if seconds > maximumSeconds {
		seconds = maximumSeconds
	}
	return configuredHysteriaUDPIdleTimeout(time.Duration(seconds) * time.Second)
}

func hysteriaUDPSessionExpired(conn *InterConn, now time.Time, timeout time.Duration) bool {
	if conn == nil || timeout <= 0 {
		return false
	}
	return hysteriaUDPActivityExpired(conn.Time(), now, timeout)
}

func hysteriaUDPActivityExpired(lastActivity, now time.Time, timeout time.Duration) bool {
	if timeout <= 0 || lastActivity.IsZero() || timeout > time.Duration(1<<63-1)-hysteriaCleanerActivityGrace {
		return false
	}
	return now.Sub(lastActivity) > timeout+hysteriaCleanerActivityGrace
}

// closeHysteriaUDPSessionLocked is called with both the manager write lock and
// the connection mutex held. The queue is already unreachable by new senders;
// draining it clears buffered byte-slice references immediately.
func (m *udpSessionManager) closeHysteriaUDPSessionLocked(udpConn *InterConn) bool {
	if udpConn == nil || udpConn.closed {
		return false
	}
	udpConn.closed = true
	close(udpConn.ch)
	queuedBytes := releaseHysteriaUDPQueue(udpConn.ch)
	delete(m.m, udpConn.id)
	notifyHysteriaCleanerMemoryRelease(1, queuedBytes, m.udpIdleTimeout)
	return true
}

// closeHysteriaUDPSessionIfExpired performs the final identity, activity and
// close decision atomically with respect to inbound and outbound activity.
// The caller already holds the manager write lock.
func (m *udpSessionManager) closeHysteriaUDPSessionIfExpired(udpConn *InterConn, now time.Time) bool {
	if udpConn == nil || m.m[udpConn.id] != udpConn {
		return false
	}
	udpConn.mutex.Lock()
	defer udpConn.mutex.Unlock()
	if udpConn.closed || !hysteriaUDPActivityExpired(udpConn.time, now, m.udpIdleTimeout) {
		return false
	}
	return m.closeHysteriaUDPSessionLocked(udpConn)
}
