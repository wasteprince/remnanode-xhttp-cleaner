package main

// This overlay provides process-wide memory maintenance for every Xray
// transport. It never closes a connection: active XHTTP, Hysteria, raw TCP,
// WebSocket and gRPC buffers stay reachable and survive both GC passes.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	apppolicy "github.com/xtls/xray-core/app/policy"
	"github.com/xtls/xray-core/app/proxyman"
	"github.com/xtls/xray-core/core"
	xraypolicy "github.com/xtls/xray-core/features/policy"
	"github.com/xtls/xray-core/transport/internet"
	xraygrpc "github.com/xtls/xray-core/transport/internet/grpc"
	xrayhysteria "github.com/xtls/xray-core/transport/internet/hysteria"
	"github.com/xtls/xray-core/transport/internet/splithttp"
)

const (
	memoryOptimizerDefaultInterval  = 5 * time.Minute
	memoryOptimizerMinimumInterval  = time.Minute
	memoryOptimizerAdaptiveFloor    = 2 * time.Minute
	memoryOptimizerActivityGrace    = 30 * time.Second
	memoryOptimizerMinimumFootprint = uint64(256 << 20)
	memoryOptimizerStatusPath       = "/tmp/xray-memory-optimizer.json"
	memoryOptimizerDefaultUDPBatch  = uint64(256)
	memoryOptimizerMinimumUDPBatch  = uint64(64)
	memoryOptimizerMaximumUDPBatch  = uint64(65536)
)

type memoryOptimizerConfig struct {
	interval       time.Duration
	minimumBytes   uint64
	memoryLimit    int64
	limitSource    string
	statusPath     string
	forceEveryTick bool
	tracker        *memoryReclaimTracker
}

type memoryOptimizerStatus struct {
	UpdatedAt                   string           `json:"updated_at"`
	ProcessID                   int              `json:"process_id"`
	Enabled                     bool             `json:"enabled"`
	IntervalSeconds             int64            `json:"interval_seconds"`
	AdaptiveIntervalSeconds     int64            `json:"adaptive_interval_seconds"`
	ActivityGraceSeconds        int64            `json:"activity_grace_seconds"`
	ObservedIdleTimeouts        map[string]int64 `json:"observed_idle_timeouts_seconds,omitempty"`
	LimitSource                 string           `json:"limit_source"`
	GoMemoryLimit               int64            `json:"go_memory_limit_bytes"`
	RuntimeInUse                uint64           `json:"runtime_in_use_bytes"`
	HeapAlloc                   uint64           `json:"heap_alloc_bytes"`
	HeapIdle                    uint64           `json:"heap_idle_bytes"`
	HeapReleased                uint64           `json:"heap_released_bytes"`
	ForcedRuns                  uint64           `json:"forced_runs"`
	LastReclaimed               uint64           `json:"last_reclaimed_bytes"`
	LastForcedAt                string           `json:"last_forced_at,omitempty"`
	LastTrigger                 string           `json:"last_trigger,omitempty"`
	UDPReclaimBatch             uint64           `json:"udp_reclaim_batch"`
	HysteriaSessionsReleased    uint64           `json:"hysteria_sessions_released"`
	HysteriaPendingSessions     uint64           `json:"hysteria_pending_sessions"`
	HysteriaQueuedBytesReleased uint64           `json:"hysteria_queued_bytes_released"`
	HysteriaReclaimRuns         uint64           `json:"hysteria_reclaim_runs"`
	TransportScope              string           `json:"transport_scope"`
	ConnectionPolicy            string           `json:"connection_policy"`
}

var (
	memoryOptimizerRuns         atomic.Uint64
	memoryOptimizerHysteriaRuns atomic.Uint64
	memoryOptimizerLastForced   atomic.Int64
	memoryOptimizerLastTrigger  atomic.Value
	activeMemoryOptimizer       atomic.Pointer[memoryReclaimTracker]
)

// memoryReclaimTracker aggregates release events. The transport hot path only
// performs atomics and a non-blocking wakeup; it never runs GC itself.
type memoryReclaimTracker struct {
	batch           uint64
	wake            chan struct{}
	configChanged   chan struct{}
	sessions        atomic.Uint64
	queuedBytes     atomic.Uint64
	pendingSessions atomic.Uint64
	timeoutMu       sync.RWMutex
	timeouts        map[string]time.Duration
}

func newMemoryReclaimTracker(batch uint64) *memoryReclaimTracker {
	return &memoryReclaimTracker{
		batch:         batch,
		wake:          make(chan struct{}, 1),
		configChanged: make(chan struct{}, 1),
		timeouts:      make(map[string]time.Duration),
	}
}

func (tracker *memoryReclaimTracker) recordHysteria(sessions, queuedBytes uint64) {
	if tracker == nil || sessions == 0 {
		return
	}
	tracker.sessions.Add(sessions)
	tracker.queuedBytes.Add(queuedBytes)
	pending := tracker.pendingSessions.Add(sessions)
	if pending >= tracker.batch {
		select {
		case tracker.wake <- struct{}{}:
		default:
		}
	}
}

func (tracker *memoryReclaimTracker) takePending() uint64 {
	if tracker == nil {
		return 0
	}
	return tracker.pendingSessions.Swap(0)
}

// observeTimeout keeps the shortest value for a named source. Config reloads
// create a new Xray process in RemnaNode, so values never need to be lengthened
// in place. Distinct listeners use distinct names.
func (tracker *memoryReclaimTracker) observeTimeout(name string, timeout time.Duration) {
	if tracker == nil || name == "" || timeout <= 0 {
		return
	}
	tracker.timeoutMu.Lock()
	previous, exists := tracker.timeouts[name]
	changed := !exists || timeout < previous
	if changed {
		tracker.timeouts[name] = timeout
	}
	tracker.timeoutMu.Unlock()
	if changed {
		select {
		case tracker.configChanged <- struct{}{}:
		default:
		}
	}
}

func (tracker *memoryReclaimTracker) timeoutSnapshot() (time.Duration, map[string]int64) {
	if tracker == nil {
		return 0, nil
	}
	tracker.timeoutMu.RLock()
	names := make([]string, 0, len(tracker.timeouts))
	for name := range tracker.timeouts {
		names = append(names, name)
	}
	sort.Strings(names)
	values := make(map[string]int64, len(names))
	var shortest time.Duration
	for _, name := range names {
		timeout := tracker.timeouts[name]
		values[name] = int64(timeout / time.Second)
		if shortest == 0 || timeout < shortest {
			shortest = timeout
		}
	}
	tracker.timeoutMu.RUnlock()
	return shortest, values
}

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

func uintFromEnv(name string, fallback, minimum, maximum uint64) uint64 {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseUint(raw, 10, 64)
	if err != nil || value < minimum || value > maximum {
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
		tracker: newMemoryReclaimTracker(uintFromEnv(
			"XRAY_MEMORY_OPTIMIZER_UDP_BATCH",
			memoryOptimizerDefaultUDPBatch,
			memoryOptimizerMinimumUDPBatch,
			memoryOptimizerMaximumUDPBatch,
		)),
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
	// RemnaNode and kernel socket buffers share the same cgroup with Xray. Keep
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

func adaptiveMemoryOptimizerInterval(configured, shortest time.Duration) time.Duration {
	if shortest <= 0 {
		return configured
	}
	candidate := shortest/2 + memoryOptimizerActivityGrace
	if candidate < memoryOptimizerAdaptiveFloor {
		candidate = memoryOptimizerAdaptiveFloor
	}
	if candidate > configured {
		return configured
	}
	return candidate
}

func durationFromPositiveSeconds(seconds int64) time.Duration {
	if seconds <= 0 {
		return 0
	}
	maximumSeconds := int64((time.Duration(1<<63 - 1)) / time.Second)
	if seconds > maximumSeconds {
		seconds = maximumSeconds
	}
	return time.Duration(seconds) * time.Second
}

func writeMemoryOptimizerStatus(config memoryOptimizerConfig, before, after runtime.MemStats, reclaimed uint64) {
	shortest, observed := config.tracker.timeoutSnapshot()
	status := memoryOptimizerStatus{
		UpdatedAt:                   time.Now().UTC().Format(time.RFC3339),
		Enabled:                     true,
		IntervalSeconds:             int64(config.interval / time.Second),
		AdaptiveIntervalSeconds:     int64(adaptiveMemoryOptimizerInterval(config.interval, shortest) / time.Second),
		ActivityGraceSeconds:        int64(memoryOptimizerActivityGrace / time.Second),
		ObservedIdleTimeouts:        observed,
		LimitSource:                 config.limitSource,
		GoMemoryLimit:               config.memoryLimit,
		RuntimeInUse:                runtimeInUse(&after),
		HeapAlloc:                   after.HeapAlloc,
		HeapIdle:                    after.HeapIdle,
		HeapReleased:                after.HeapReleased,
		ForcedRuns:                  memoryOptimizerRuns.Load(),
		LastReclaimed:               reclaimed,
		ProcessID:                   os.Getpid(),
		UDPReclaimBatch:             config.tracker.batch,
		HysteriaSessionsReleased:    config.tracker.sessions.Load(),
		HysteriaPendingSessions:     config.tracker.pendingSessions.Load(),
		HysteriaQueuedBytesReleased: config.tracker.queuedBytes.Load(),
		HysteriaReclaimRuns:         memoryOptimizerHysteriaRuns.Load(),
		TransportScope:              "xhttp,tcp,websocket,grpc,hysteria",
		ConnectionPolicy:            "config-aware,activity-safe,never-close-active-for-gc",
	}
	if forcedAt := memoryOptimizerLastForced.Load(); forcedAt > 0 {
		status.LastForcedAt = time.Unix(0, forcedAt).UTC().Format(time.RFC3339)
	}
	if value := memoryOptimizerLastTrigger.Load(); value != nil {
		status.LastTrigger = value.(string)
	}
	payload, err := json.Marshal(status)
	if err != nil {
		return
	}
	temporary, err := os.CreateTemp(
		filepath.Dir(config.statusPath),
		"."+filepath.Base(config.statusPath)+"-*.tmp",
	)
	if err != nil {
		return
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if _, err := temporary.Write(append(payload, '\n')); err != nil {
		temporary.Close()
		return
	}
	if err := temporary.Close(); err != nil {
		return
	}
	_ = os.Rename(temporaryName, config.statusPath)
}

func runMemoryOptimizerCycle(config memoryOptimizerConfig, trigger string) bool {
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	if !config.forceEveryTick && runtimeInUse(&before) < config.minimumBytes {
		writeMemoryOptimizerStatus(config, before, before, 0)
		return false
	}

	// Two collections are deliberate. sync.Pool keeps the previous generation
	// as a victim cache for one GC cycle. The first GC rotates the pools; the
	// FreeOSMemory call performs another GC and scavenges unused pages to Linux.
	// Objects referenced by a live transport remain reachable and survive both.
	runtime.GC()
	debug.FreeOSMemory()
	memoryOptimizerRuns.Add(1)
	memoryOptimizerLastForced.Store(time.Now().UnixNano())
	memoryOptimizerLastTrigger.Store(trigger)
	if trigger == "hysteria-udp-release-batch" {
		memoryOptimizerHysteriaRuns.Add(1)
	}

	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	beforeInUse, afterInUse := runtimeInUse(&before), runtimeInUse(&after)
	var reclaimed uint64
	if beforeInUse > afterInUse {
		reclaimed = beforeInUse - afterInUse
	}
	writeMemoryOptimizerStatus(config, before, after, reclaimed)
	return true
}

func resetMemoryOptimizerTimer(timer *time.Timer, interval time.Duration) {
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
	timer.Reset(interval)
}

func runMemoryOptimizerLoop(config memoryOptimizerConfig) {
	shortest, _ := config.tracker.timeoutSnapshot()
	periodic := time.NewTimer(adaptiveMemoryOptimizerInterval(config.interval, shortest))
	defer periodic.Stop()

	var reclaim *time.Timer
	var reclaimC <-chan time.Time
	for {
		select {
		case <-periodic.C:
			if runMemoryOptimizerCycle(config, "config-aware-periodic") {
				config.tracker.takePending()
			}
			shortest, _ = config.tracker.timeoutSnapshot()
			periodic.Reset(adaptiveMemoryOptimizerInterval(config.interval, shortest))
		case <-config.tracker.configChanged:
			shortest, _ = config.tracker.timeoutSnapshot()
			resetMemoryOptimizerTimer(periodic, adaptiveMemoryOptimizerInterval(config.interval, shortest))
			var stats runtime.MemStats
			runtime.ReadMemStats(&stats)
			writeMemoryOptimizerStatus(config, stats, stats, 0)
		case <-config.tracker.wake:
			if config.tracker.pendingSessions.Load() < config.tracker.batch || reclaimC != nil {
				continue
			}
			// Give final dispatcher references and in-flight reads a full activity
			// grace window before asking Go to reclaim detached UDP objects.
			reclaim = time.NewTimer(memoryOptimizerActivityGrace)
			reclaimC = reclaim.C
		case <-reclaimC:
			reclaimC = nil
			pending := config.tracker.takePending()
			if pending == 0 {
				continue
			}
			shortest, _ = config.tracker.timeoutSnapshot()
			cooldown := adaptiveMemoryOptimizerInterval(config.interval, shortest)
			lastForced := memoryOptimizerLastForced.Load()
			if lastForced > 0 {
				remaining := time.Until(time.Unix(0, lastForced).Add(cooldown))
				if remaining > 0 {
					config.tracker.pendingSessions.Add(pending)
					reclaim = time.NewTimer(remaining)
					reclaimC = reclaim.C
					continue
				}
			}
			runMemoryOptimizerCycle(config, "hysteria-udp-release-batch")
		}
	}
}

func startMemoryOptimizer() {
	if !memoryOptimizerEnabled() {
		return
	}
	config := loadMemoryOptimizerConfig()
	activeMemoryOptimizer.Store(config.tracker)
	xrayhysteria.SetCleanerMemoryReleaseHook(func(release xrayhysteria.CleanerMemoryRelease) {
		if release.Sessions == 0 && release.IdleTimeoutSeconds > 0 {
			config.tracker.observeTimeout("hysteria-udp-runtime", time.Duration(release.IdleTimeoutSeconds)*time.Second)
			return
		}
		config.tracker.recordHysteria(release.Sessions, release.QueuedBytes)
	})
	var initial runtime.MemStats
	runtime.ReadMemStats(&initial)
	writeMemoryOptimizerStatus(config, initial, initial, 0)
	go runMemoryOptimizerLoop(config)
}

func observeStreamTimeouts(tracker *memoryReclaimTracker, prefix string, stream *internet.StreamConfig) {
	if tracker == nil || stream == nil {
		return
	}
	protocol := strings.ToLower(stream.GetProtocolName())
	if protocol == "splithttp" || protocol == "xhttp" {
		tracker.observeTimeout(prefix+"-xhttp-session", splithttp.CleanerIdleTimeout())
	}
	if (protocol == "hysteria" || protocol == "splithttp") && stream.GetQuicParams() != nil {
		idle := stream.GetQuicParams().GetMaxIdleTimeout()
		if idle == 0 && protocol == "hysteria" {
			idle = 30
		}
		if idle > 0 {
			tracker.observeTimeout(prefix+"-quic", durationFromPositiveSeconds(idle))
		}
	}
	for _, transport := range stream.GetTransportSettings() {
		if transport == nil || transport.GetSettings() == nil || strings.ToLower(transport.GetProtocolName()) != protocol {
			continue
		}
		instance, err := transport.GetSettings().GetInstance()
		if err != nil {
			continue
		}
		switch settings := instance.(type) {
		case *splithttp.Config:
			if xmux := settings.GetXmux(); xmux != nil {
				if lifetime := xmux.GetHMaxReusableSecs(); lifetime != nil && lifetime.GetFrom() > 0 {
					tracker.observeTimeout(prefix+"-xhttp-reusable", time.Duration(lifetime.GetFrom())*time.Second)
				}
			}
			if streamUp := settings.GetScStreamUpServerSecs(); streamUp != nil && streamUp.GetFrom() > 0 {
				tracker.observeTimeout(prefix+"-xhttp-stream-up", time.Duration(streamUp.GetFrom())*time.Second)
			}
		case *xraygrpc.Config:
			if settings.GetIdleTimeout() > 0 {
				tracker.observeTimeout(prefix+"-grpc", durationFromPositiveSeconds(int64(settings.GetIdleTimeout())))
			}
		case *xrayhysteria.Config:
			idle := settings.GetUdpIdleTimeout()
			if idle <= 0 {
				idle = 60
			}
			tracker.observeTimeout(prefix+"-hysteria-udp", durationFromPositiveSeconds(idle))
		}
	}
}

// observeMemoryOptimizerConfig reads the already parsed Xray protobuf. It does
// not parse JSON independently and therefore follows merged configs, defaults
// and policy levels exactly as the running instance sees them.
func observeMemoryOptimizerConfig(config *core.Config, server *core.Instance) {
	tracker := activeMemoryOptimizer.Load()
	if tracker == nil || config == nil || server == nil {
		return
	}
	if feature := server.GetFeature(xraypolicy.ManagerType()); feature != nil {
		if manager, ok := feature.(xraypolicy.Manager); ok {
			levels := map[uint32]struct{}{0: {}, 1: {}}
			for _, app := range config.GetApp() {
				if app == nil {
					continue
				}
				instance, err := app.GetInstance()
				if err != nil {
					continue
				}
				if policyConfig, ok := instance.(*apppolicy.Config); ok {
					for level := range policyConfig.GetLevel() {
						levels[level] = struct{}{}
					}
				}
			}
			for level := range levels {
				tracker.observeTimeout(fmt.Sprintf("policy-level-%d", level), manager.ForLevel(level).Timeouts.ConnectionIdle)
			}
		}
	}
	for index, inbound := range config.GetInbound() {
		if inbound == nil || inbound.GetReceiverSettings() == nil {
			continue
		}
		instance, err := inbound.GetReceiverSettings().GetInstance()
		if err != nil {
			continue
		}
		if receiver, ok := instance.(*proxyman.ReceiverConfig); ok {
			observeStreamTimeouts(tracker, fmt.Sprintf("inbound-%d", index), receiver.GetStreamSettings())
		}
	}
	for index, outbound := range config.GetOutbound() {
		if outbound == nil || outbound.GetSenderSettings() == nil {
			continue
		}
		instance, err := outbound.GetSenderSettings().GetInstance()
		if err != nil {
			continue
		}
		if sender, ok := instance.(*proxyman.SenderConfig); ok {
			observeStreamTimeouts(tracker, fmt.Sprintf("outbound-%d", index), sender.GetStreamSettings())
		}
	}
}

func memoryOptimizerServerCommand(args []string) bool {
	if len(args) < 2 || args[1] != "run" {
		return false
	}
	for _, argument := range args[2:] {
		name := strings.SplitN(argument, "=", 2)[0]
		if name == "-test" || name == "--test" || name == "-dump" || name == "--dump" {
			return false
		}
	}
	return true
}

func startMemoryOptimizerForCommand(args []string) {
	if memoryOptimizerServerCommand(args) {
		startMemoryOptimizer()
	}
}
