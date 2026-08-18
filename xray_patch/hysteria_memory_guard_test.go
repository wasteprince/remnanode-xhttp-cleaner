package hysteria

import (
	"testing"
	"time"
)

func TestHysteriaCleanerBoundsOnlyLongIdleTimeouts(t *testing.T) {
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "")
	if got := boundedHysteriaUDPIdleTimeout(60 * time.Second); got != 60*time.Second {
		t.Fatalf("upstream default changed: got %s", got)
	}
	if got := boundedHysteriaUDPIdleTimeout(10 * time.Minute); got != 2*time.Minute {
		t.Fatalf("long idle timeout was not capped: got %s", got)
	}
	if got := boundedHysteriaUDPIdleTimeout(0); got != time.Minute {
		t.Fatalf("empty timeout did not preserve upstream default: got %s", got)
	}
}

func TestHysteriaCleanerIdleCapCanBeTunedOrDisabled(t *testing.T) {
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "90s")
	if got := boundedHysteriaUDPIdleTimeout(10 * time.Minute); got != 90*time.Second {
		t.Fatalf("configured cap ignored: got %s", got)
	}
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "off")
	if got := boundedHysteriaUDPIdleTimeout(10 * time.Minute); got != 10*time.Minute {
		t.Fatalf("disabled cap changed timeout: got %s", got)
	}
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "1s")
	if got := boundedHysteriaUDPIdleTimeout(10 * time.Minute); got != 2*time.Minute {
		t.Fatalf("unsafe cap did not fail closed to the default: got %s", got)
	}
}

func TestHysteriaCleanerRechecksActivityBeforeExpiry(t *testing.T) {
	now := time.Now()
	conn := &InterConn{time: now.Add(-3 * time.Minute)}
	if !hysteriaUDPSessionExpired(conn, now, 2*time.Minute) {
		t.Fatal("inactive session was not considered expired")
	}
	conn.Update()
	if hysteriaUDPSessionExpired(conn, time.Now(), 2*time.Minute) {
		t.Fatal("new activity was ignored before expiry")
	}
}
