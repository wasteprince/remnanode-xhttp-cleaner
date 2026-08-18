package hysteria

// This overlay bounds how long an inactive logical Hysteria UDP session keeps
// its dispatcher, outbound socket and transport pipes reachable. It does not
// close an active QUIC connection and it does not change QUIC flow-control or
// packet queue sizes. An expired logical UDP session is recreated by the
// existing server feed path if the client sends its session ID again.

import (
	"os"
	"strings"
	"time"
)

const (
	hysteriaCleanerDefaultUDPIdleCap = 2 * time.Minute
	hysteriaCleanerMinimumUDPIdleCap = 30 * time.Second
	hysteriaCleanerMaximumUDPIdleCap = 10 * time.Minute
)

func hysteriaCleanerUDPIdleCap() time.Duration {
	raw := strings.TrimSpace(os.Getenv("XRAY_HYSTERIA_UDP_IDLE_CAP"))
	if raw == "" {
		return hysteriaCleanerDefaultUDPIdleCap
	}
	switch strings.ToLower(raw) {
	case "0", "false", "off", "no":
		return 0
	}
	value, err := time.ParseDuration(raw)
	if err != nil || value < hysteriaCleanerMinimumUDPIdleCap || value > hysteriaCleanerMaximumUDPIdleCap {
		return hysteriaCleanerDefaultUDPIdleCap
	}
	return value
}

func boundedHysteriaUDPIdleTimeout(configured time.Duration) time.Duration {
	// Upstream uses 60 seconds when the JSON field is omitted. Preserve that
	// behavior for defensive callers which pass an empty protobuf value.
	if configured <= 0 {
		configured = time.Minute
	}
	cap := hysteriaCleanerUDPIdleCap()
	if cap > 0 && configured > cap {
		return cap
	}
	return configured
}

func hysteriaUDPSessionExpired(conn *InterConn, now time.Time, timeout time.Duration) bool {
	if conn == nil || timeout <= 0 {
		return false
	}
	lastActivity := conn.Time()
	return !lastActivity.IsZero() && now.Sub(lastActivity) > timeout
}
