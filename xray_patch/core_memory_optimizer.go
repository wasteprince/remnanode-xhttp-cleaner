package main

// This overlay provides process-wide memory maintenance for every Xray
// transport. It never closes a connection: active XHTTP, raw TCP and gRPC
// buffers stay reachable and therefore survive both GC passes.

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

const (
	memoryOptimizerDefaultInterval = 5 * time.Minute
	memoryOptimizerMinimumInterval = time.Minute
	memoryOptimizerMinimumFootprint = uint64(256 << 20)
	memoryOptimizerStatusPath       = "/tmp/xray-memory-optimizer.json"
)

type memoryOptimizerConfig struct {
	interval       time.Duration
	minimumBytes   uint64
	memoryLimit    int64
	limitSource    string
	statusPath     string
	forceEveryTick bool
}

type memoryOptimizerStatus struct {
	UpdatedAt        string `json:"updated_at"`
	Enabled          bool   `json:"enabled"`
	IntervalSeconds  int64  `json:"interval_seconds"`
	LimitSource      string `json:"limit_source"`
	GoMemoryLimit    int64  `json:"go_memory_limit_bytes"`
	RuntimeInUse     uint64 `json:"runtime_in_use_bytes"`
	HeapAlloc        uint64 `json:"heap_alloc_bytes"`
	HeapIdle         uint64 `json:"heap_idle_bytes"`
	HeapReleased     uint64 `json:"heap_released_bytes"`
	ForcedRuns       uint64 `json:"forced_runs"`
	LastReclaimed    uint64 `json:"last_reclaimed_bytes"`
	LastForcedAt     string `json:"last_forced_at,omitempty"`
	TransportScope   string `json:"transport_scope"`
	ConnectionPolicy string `json:"connection_policy"`
}

var memoryOptimizerRuns atomic.Uint64

func parseMemoryBytes(raw string) (uint64, bool) {
	value := strings.TrimSpace(strings.ToUpper(raw))
	if value == "" || value == "MAX" {
		return 0, false
	}
	multiplier := uint64(1)
	for _, suffix := range []struct {
		name string
		mul  uint64
	}{{"GIB", 1 << 30}, {"MIB", 1 << 20}, {"KIB", 1 << 10}, {"GB", 1_000_000_000}, {"MB", 1_000_000}, {"KB", 1_000}, {"B", 1}} {
		if strings.HasSuffix(value, suffix.name) {
			value = strings.TrimSpace(strings.TrimSuffix(value, suffix.name))
			multiplier = suffix.mul
			break
		}
	}
	number, err := strconv.ParseUint(value, 10, 64)
	if err != nil || number == 0 || number > ^uint64(0)/multiplier {
		return 0, false
	}
	return number * multiplier, true
}

func readMemoryValue(path string) (uint64, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	return parseMemoryBytes(string(data))
}

func physicalMemoryBytes() (uint64, bool) {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0, false
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[0] == "MemTotal:" {
			value, err := strconv.ParseUint(fields[1], 10, 64)
			return value << 10, err == nil && value > 0
		}
	}
	return 0, false
}

func effectiveMemoryCeiling() (uint64, string) {
	var ceiling uint64
	source := "unknown"
	for _, candidate := range []struct {
		path   string
		source string
	}{
		{"/sys/fs/cgroup/memory.max", "cgroup-v2"},
		{"/sys/fs/cgroup/memory/memory.limit_in_bytes", "cgroup-v1"},
	} {
		if value, ok := readMemoryValue(candidate.path); ok && (ceiling == 0 || value < ceiling) {
			ceiling, source = value, candidate.source
		}
	}
	if value, ok := physicalMemoryBytes(); ok && (ceiling == 0 || value < ceiling) {
		ceiling, source = value, "host"
	}
	return ceiling, source
}

func durationFromEnv(name string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := time.ParseDuration(raw)
	if err != nil || value < memoryOptimizerMinimumInterval {
		return fallback
	}
	return value
}

func memoryOptimizerEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("XRAY_MEMORY_OPTIMIZER"))) {
	case "0", "false", "off", "no":
		return false
	default:
		return true
	}
}

func loadMemoryOptimizerConfig() memoryOptimizerConfig {
	config := memoryOptimizerConfig{
		interval:   durationFromEnv("XRAY_MEMORY_OPTIMIZER_INTERVAL", memoryOptimizerDefaultInterval),
		statusPath: strings.TrimSpace(os.Getenv("XRAY_MEMORY_OPTIMIZER_STATUS")),
	}
	if config.statusPath == "" {
		config.statusPath = memoryOptimizerStatusPath
	}
	config.forceEveryTick = strings.EqualFold(strings.TrimSpace(os.Getenv("XRAY_MEMORY_OPTIMIZER_FORCE")), "true")

	ceiling, ceilingSource := effectiveMemoryCeiling()
	config.minimumBytes = memoryOptimizerMinimumFootprint
	if ceiling/16 > config.minimumBytes {
		config.minimumBytes = ceiling / 16
	}
	if explicit, ok := parseMemoryBytes(os.Getenv("XRAY_MEMORY_OPTIMIZER_MIN_BYTES")); ok {
		config.minimumBytes = explicit
	}

	currentLimit := debug.SetMemoryLimit(-1)
	if os.Getenv("GOMEMLIMIT") != "" {
		config.memoryLimit = currentLimit
		config.limitSource = "GOMEMLIMIT"
		return config
	}
	maxInt64 := uint64(^uint64(0) >> 1)
	if explicit, ok := parseMemoryBytes(os.Getenv("XRAY_MEMORY_LIMIT")); ok && explicit <= maxInt64 {
		config.memoryLimit = int64(explicit)
		config.limitSource = "XRAY_MEMORY_LIMIT"
		debug.SetMemoryLimit(config.memoryLimit)
		return config
	}
	// RemnaNode and kernel TCP buffers share the same cgroup with Xray. Keep
	// 30% outside the Go runtime instead of consuming the whole container cap.
	if ceiling >= 512<<20 {
		limit := ceiling / 10 * 7
		if limit >= 256<<20 && limit <= maxInt64 {
			config.memoryLimit = int64(limit)
			config.limitSource = "auto-" + ceilingSource + "-70pct"
			debug.SetMemoryLimit(config.memoryLimit)
			return config
		}
	}
	config.memoryLimit = currentLimit
	config.limitSource = "go-default"
	return config
}

func runtimeInUse(stats *runtime.MemStats) uint64 {
	if stats.Sys <= stats.HeapReleased {
		return 0
	}
	return stats.Sys - stats.HeapReleased
}

func writeMemoryOptimizerStatus(config memoryOptimizerConfig, before, after runtime.MemStats, reclaimed uint64, forced bool) {
	status := memoryOptimizerStatus{
		UpdatedAt:        time.Now().UTC().Format(time.RFC3339),
		Enabled:          true,
		IntervalSeconds:  int64(config.interval / time.Second),
		LimitSource:      config.limitSource,
		GoMemoryLimit:    config.memoryLimit,
		RuntimeInUse:     runtimeInUse(&after),
		HeapAlloc:        after.HeapAlloc,
		HeapIdle:         after.HeapIdle,
		HeapReleased:     after.HeapReleased,
		ForcedRuns:       memoryOptimizerRuns.Load(),
		LastReclaimed:    reclaimed,
		TransportScope:   "xhttp,tcp,grpc",
		ConnectionPolicy: "never-close-active",
	}
	if forced {
		status.LastForcedAt = status.UpdatedAt
	}
	payload, err := json.Marshal(status)
	if err != nil {
		return
	}
	temporary := fmt.Sprintf("%s.%d.tmp", config.statusPath, os.Getpid())
	if err := os.WriteFile(temporary, append(payload, '\n'), 0o600); err != nil {
		return
	}
	_ = os.Rename(temporary, config.statusPath)
}

func runMemoryOptimizerCycle(config memoryOptimizerConfig) {
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	if !config.forceEveryTick && runtimeInUse(&before) < config.minimumBytes {
		writeMemoryOptimizerStatus(config, before, before, 0, false)
		return
	}

	// Two collections are deliberate. sync.Pool keeps the previous generation
	// as a victim cache for one GC cycle. The first GC rotates the pools; the
	// FreeOSMemory call performs another GC and scavenges unused pages to Linux.
	// Objects referenced by a live TCP bridge or gRPC/XHTTP stream survive both.
	runtime.GC()
	debug.FreeOSMemory()
	memoryOptimizerRuns.Add(1)

	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	beforeInUse, afterInUse := runtimeInUse(&before), runtimeInUse(&after)
	var reclaimed uint64
	if beforeInUse > afterInUse {
		reclaimed = beforeInUse - afterInUse
	}
	writeMemoryOptimizerStatus(config, before, after, reclaimed, true)
}

func startMemoryOptimizer() {
	if !memoryOptimizerEnabled() {
		return
	}
	config := loadMemoryOptimizerConfig()
	var initial runtime.MemStats
	runtime.ReadMemStats(&initial)
	writeMemoryOptimizerStatus(config, initial, initial, 0, false)
	go func() {
		ticker := time.NewTicker(config.interval)
		defer ticker.Stop()
		for range ticker.C {
			runMemoryOptimizerCycle(config)
		}
	}()
}

func init() {
	startMemoryOptimizer()
}
