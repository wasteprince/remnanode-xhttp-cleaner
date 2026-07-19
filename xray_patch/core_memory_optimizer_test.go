package main

import (
	"runtime"
	"testing"
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

func TestMemoryOptimizerRuntimeInUseCannotUnderflow(t *testing.T) {
	if got := runtimeInUse(&runtime.MemStats{Sys: 10, HeapReleased: 20}); got != 0 {
		t.Fatalf("runtimeInUse underflowed: %d", got)
	}
	if got := runtimeInUse(&runtime.MemStats{Sys: 100, HeapReleased: 30}); got != 70 {
		t.Fatalf("runtimeInUse = %d; want 70", got)
	}
}
