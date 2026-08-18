package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/xtls/xray-core/common/serial"
	"github.com/xtls/xray-core/core"
	"github.com/xtls/xray-core/transport/internet"
	xraygrpc "github.com/xtls/xray-core/transport/internet/grpc"
	xrayhysteria "github.com/xtls/xray-core/transport/internet/hysteria"
	"github.com/xtls/xray-core/transport/internet/splithttp"
)

func TestMemoryOptimizerParsesBinaryAndDecimalSizes(t *testing.T) {
	tests := map[string]uint64{
		"256MiB": 256 << 20,
		"2GiB":   2 << 30,
		"64 MB":  64_000_000,
		"4096":   4096,
	}
	for input, expected := range tests {
		actual, ok := parseMemoryBytes(input)
		if !ok || actual != expected {
			t.Fatalf("parseMemoryBytes(%q) = %d, %v; want %d, true", input, actual, ok, expected)
		}
	}
	if _, ok := parseMemoryBytes("max"); ok {
		t.Fatal("cgroup max must not be parsed as a finite memory limit")
	}
}

func TestMemoryOptimizerAdaptiveIntervalUsesConfiguredTimeoutsSafely(t *testing.T) {
	tests := []struct {
		name       string
		configured time.Duration
		shortest   time.Duration
		want       time.Duration
	}{
		{"no observed timeout", 5 * time.Minute, 0, 5 * time.Minute},
		{"short QUIC timeout respects CPU floor", 5 * time.Minute, 20 * time.Second, 2 * time.Minute},
		{"policy timeout plus grace", 5 * time.Minute, 5 * time.Minute, 3 * time.Minute},
		{"long timeout keeps configured cadence", 5 * time.Minute, 10 * time.Minute, 5 * time.Minute},
		{"explicit faster cadence wins", time.Minute, 20 * time.Second, time.Minute},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := adaptiveMemoryOptimizerInterval(test.configured, test.shortest); got != test.want {
				t.Fatalf("adaptive interval = %s; want %s", got, test.want)
			}
		})
	}
}

func TestMemoryOptimizerSecondsConversionCannotOverflow(t *testing.T) {
	if got := durationFromPositiveSeconds(1<<63 - 1); got <= 0 {
		t.Fatalf("large second value overflowed to %s", got)
	}
	if got := durationFromPositiveSeconds(0); got != 0 {
		t.Fatalf("zero seconds became %s", got)
	}
}

func TestMemoryOptimizerBatchesHysteriaReleaseEvents(t *testing.T) {
	tracker := newMemoryReclaimTracker(2)
	tracker.recordHysteria(1, 128)
	select {
	case <-tracker.wake:
		t.Fatal("single release woke a two-session batch")
	default:
	}
	tracker.recordHysteria(1, 256)
	select {
	case <-tracker.wake:
	default:
		t.Fatal("complete release batch did not wake optimizer")
	}
	if got := tracker.takePending(); got != 2 {
		t.Fatalf("pending sessions = %d; want 2", got)
	}
	if got := tracker.queuedBytes.Load(); got != 384 {
		t.Fatalf("released queue bytes = %d; want 384", got)
	}
}

func TestMemoryOptimizerObservesTransportSpecificTimeouts(t *testing.T) {
	tracker := newMemoryReclaimTracker(memoryOptimizerDefaultUDPBatch)
	stream := &internet.StreamConfig{
		ProtocolName: "hysteria",
		QuicParams:   &internet.QuicParams{MaxIdleTimeout: 20},
		TransportSettings: []*internet.TransportConfig{{
			ProtocolName: "hysteria",
			Settings: serial.ToTypedMessage(&xrayhysteria.Config{
				UdpIdleTimeout: 600,
			}),
		}},
	}
	observeStreamTimeouts(tracker, "inbound-0", stream)
	shortest, observed := tracker.timeoutSnapshot()
	if shortest != 20*time.Second {
		t.Fatalf("shortest observed timeout = %s; want 20s", shortest)
	}
	if observed["inbound-0-hysteria-udp"] != 600 || observed["inbound-0-quic"] != 20 {
		t.Fatalf("unexpected Hysteria observations: %#v", observed)
	}

	grpcStream := &internet.StreamConfig{
		ProtocolName: "grpc",
		TransportSettings: []*internet.TransportConfig{{
			ProtocolName: "grpc",
			Settings:     serial.ToTypedMessage(&xraygrpc.Config{IdleTimeout: 90}),
		}},
	}
	observeStreamTimeouts(tracker, "outbound-0", grpcStream)
	_, observed = tracker.timeoutSnapshot()
	if observed["outbound-0-grpc"] != 90 {
		t.Fatalf("gRPC timeout was not observed: %#v", observed)
	}

	xhttpStream := &internet.StreamConfig{
		ProtocolName: "splithttp",
		TransportSettings: []*internet.TransportConfig{{
			ProtocolName: "splithttp",
			Settings: serial.ToTypedMessage(&splithttp.Config{Xmux: &splithttp.XmuxConfig{
				HMaxReusableSecs: &splithttp.RangeConfig{From: 600, To: 900},
			}}),
		}},
	}
	observeStreamTimeouts(tracker, "outbound-1", xhttpStream)
	_, observed = tracker.timeoutSnapshot()
	if observed["outbound-1-xhttp-reusable"] != 600 ||
		observed["outbound-1-xhttp-session"] != int64(splithttp.CleanerIdleTimeout()/time.Second) {
		t.Fatalf("XHTTP timeouts were not observed: %#v", observed)
	}
}

func TestMemoryOptimizerObservesEffectivePolicyManager(t *testing.T) {
	server, err := core.New(&core.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	tracker := newMemoryReclaimTracker(memoryOptimizerDefaultUDPBatch)
	previous := activeMemoryOptimizer.Load()
	activeMemoryOptimizer.Store(tracker)
	defer activeMemoryOptimizer.Store(previous)
	observeMemoryOptimizerConfig(&core.Config{}, server)
	_, observed := tracker.timeoutSnapshot()
	if observed["policy-level-0"] != 300 || observed["policy-level-1"] != 600 {
		t.Fatalf("effective default policies were not observed: %#v", observed)
	}
}

func TestMemoryOptimizerRuntimeInUseCannotUnderflow(t *testing.T) {
	if got := runtimeInUse(&runtime.MemStats{Sys: 10, HeapReleased: 20}); got != 0 {
		t.Fatalf("runtimeInUse underflowed: %d", got)
	}
	if got := runtimeInUse(&runtime.MemStats{Sys: 100, HeapReleased: 30}); got != 70 {
		t.Fatalf("runtimeInUse = %d; want 70", got)
	}
}

func TestMemoryOptimizerStatusIsWrittenPrivatelyAndAtomically(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status.json")
	config := memoryOptimizerConfig{
		interval:   memoryOptimizerDefaultInterval,
		statusPath: path,
		tracker:    newMemoryReclaimTracker(memoryOptimizerDefaultUDPBatch),
	}
	stats := runtime.MemStats{Sys: 1024, HeapReleased: 256}
	writeMemoryOptimizerStatus(config, stats, stats, 0)
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("status permissions = %o; want 600", info.Mode().Perm())
	}
	matches, err := filepath.Glob(filepath.Join(filepath.Dir(path), ".status.json-*.tmp"))
	if err != nil || len(matches) != 0 {
		t.Fatalf("temporary status files remain: %v, %v", matches, err)
	}
}

func TestMemoryOptimizerRunsOnlyForLongLivedServerCommand(t *testing.T) {
	tests := []struct {
		args []string
		want bool
	}{
		{[]string{"xray", "run", "-c", "config.json"}, true},
		{[]string{"xray", "run"}, true},
		{[]string{"xray", "version"}, false},
		{[]string{"xray", "run", "-test", "-c", "config.json"}, false},
		{[]string{"xray", "run", "-dump=true"}, false},
		{[]string{"xray"}, false},
	}
	for _, test := range tests {
		if got := memoryOptimizerServerCommand(test.args); got != test.want {
			t.Fatalf("memoryOptimizerServerCommand(%q) = %v; want %v", test.args, got, test.want)
		}
	}
}
