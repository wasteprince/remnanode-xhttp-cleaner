package hysteria

import (
	"testing"
	"time"
)

func TestHysteriaCleanerPreservesConfiguredIdleTimeoutByDefault(t *testing.T) {
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "")
	if got := configuredHysteriaUDPIdleTimeout(60 * time.Second); got != 60*time.Second {
		t.Fatalf("upstream default changed: got %s", got)
	}
	if got := configuredHysteriaUDPIdleTimeout(10 * time.Minute); got != 10*time.Minute {
		t.Fatalf("configured timeout changed: got %s", got)
	}
	if got := configuredHysteriaUDPIdleTimeout(0); got != time.Minute {
		t.Fatalf("empty timeout did not preserve upstream default: got %s", got)
	}
}

func TestHysteriaCleanerIdleCapCanBeTunedOrDisabled(t *testing.T) {
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "90s")
	if got := configuredHysteriaUDPIdleTimeout(10 * time.Minute); got != 90*time.Second {
		t.Fatalf("configured cap ignored: got %s", got)
	}
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "off")
	if got := configuredHysteriaUDPIdleTimeout(10 * time.Minute); got != 10*time.Minute {
		t.Fatalf("disabled cap changed timeout: got %s", got)
	}
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "1s")
	if got := configuredHysteriaUDPIdleTimeout(10 * time.Minute); got != 10*time.Minute {
		t.Fatalf("invalid cap changed the configured timeout: got %s", got)
	}
}

func TestHysteriaCleanerSecondsConversionCannotOverflow(t *testing.T) {
	t.Setenv("XRAY_HYSTERIA_UDP_IDLE_CAP", "")
	if got := configuredHysteriaUDPIdleTimeoutSeconds(600); got != 10*time.Minute {
		t.Fatalf("seconds conversion = %s; want 10m", got)
	}
	if got := configuredHysteriaUDPIdleTimeoutSeconds(1<<63 - 1); got <= 0 {
		t.Fatalf("large timeout overflowed to %s", got)
	}
}

func TestHysteriaCleanerRechecksActivityBeforeExpiry(t *testing.T) {
	now := time.Now()
	conn := &InterConn{time: now.Add(-3*time.Minute - hysteriaCleanerActivityGrace)}
	if !hysteriaUDPSessionExpired(conn, now, 2*time.Minute) {
		t.Fatal("inactive session was not considered expired")
	}
	conn.Update()
	if hysteriaUDPSessionExpired(conn, time.Now(), 2*time.Minute) {
		t.Fatal("new activity was ignored before expiry")
	}
}

func TestHysteriaCleanerAppliesActivityGrace(t *testing.T) {
	now := time.Now()
	conn := &InterConn{time: now.Add(-2*time.Minute - hysteriaCleanerActivityGrace/2)}
	if hysteriaUDPSessionExpired(conn, now, 2*time.Minute) {
		t.Fatal("session was expired during the 30-second activity grace")
	}
}

func TestHysteriaCleanerReleasesQueuedPayloadReferences(t *testing.T) {
	queue := make(chan []byte, 4)
	queue <- make([]byte, 10, 64)
	queue <- make([]byte, 20, 128)
	close(queue)
	if got := releaseHysteriaUDPQueue(queue); got != 192 {
		t.Fatalf("released queue capacity = %d; want 192", got)
	}
	if len(queue) != 0 {
		t.Fatalf("queue still retains %d payloads", len(queue))
	}
}

func TestHysteriaCleanerCloseDrainsQueueAndDetachesSession(t *testing.T) {
	queue := make(chan []byte, 2)
	queue <- make([]byte, 32, 128)
	conn := &InterConn{id: 7, ch: queue, time: time.Now().Add(-time.Hour)}
	manager := &udpSessionManager{
		m:              map[uint32]*InterConn{7: conn},
		udpIdleTimeout: time.Minute,
	}
	manager.Lock()
	closed := manager.closeHysteriaUDPSessionIfExpired(conn, time.Now())
	manager.Unlock()
	if !closed || !conn.closed {
		t.Fatal("expired UDP session was not closed")
	}
	if _, exists := manager.m[7]; exists {
		t.Fatal("closed UDP session remains in manager map")
	}
	if len(queue) != 0 {
		t.Fatal("closed UDP queue still retains payload references")
	}
}

func TestHysteriaCleanerOutboundActivityWinsCloseRace(t *testing.T) {
	enteredWrite := make(chan struct{})
	releaseWrite := make(chan struct{})
	conn := &InterConn{
		id:   9,
		ch:   make(chan []byte, 1),
		time: time.Now().Add(-time.Hour),
		write: func([]byte) error {
			close(enteredWrite)
			<-releaseWrite
			return nil
		},
	}
	manager := &udpSessionManager{
		m:              map[uint32]*InterConn{9: conn},
		udpIdleTimeout: time.Minute,
	}
	writeDone := make(chan error, 1)
	go func() {
		_, err := conn.Write(make([]byte, 4))
		writeDone <- err
	}()
	<-enteredWrite
	closeResult := make(chan bool, 1)
	go func() {
		manager.Lock()
		closeResult <- manager.closeHysteriaUDPSessionIfExpired(conn, time.Now())
		manager.Unlock()
	}()
	close(releaseWrite)
	if err := <-writeDone; err != nil {
		t.Fatal(err)
	}
	if <-closeResult {
		t.Fatal("session was closed after concurrent outbound activity")
	}
	if conn.closed {
		t.Fatal("active session is marked closed")
	}
}

func TestHysteriaCleanerReportsReleasedSessionsWithoutBlocking(t *testing.T) {
	events := make(chan CleanerMemoryRelease, 1)
	SetCleanerMemoryReleaseHook(func(event CleanerMemoryRelease) {
		select {
		case events <- event:
		default:
		}
	})
	notifyHysteriaCleanerMemoryRelease(1, 4096, 10*time.Minute)
	select {
	case event := <-events:
		if event.Sessions != 1 || event.QueuedBytes != 4096 || event.IdleTimeoutSeconds != 600 {
			t.Fatalf("unexpected release event: %+v", event)
		}
	default:
		t.Fatal("release event was not reported")
	}
}
