package semver

import "testing"

func TestCompare(t *testing.T) {
	tests := []struct {
		name string
		a, b string
		want int
	}{
		{"equal", "1.2.3", "1.2.3", 0},
		{"lower patch", "1.2.3", "1.2.4", -1},
		{"minor compares numerically", "1.10.0", "1.9.0", 1},
		{"major compares numerically", "2.0.0", "10.0.0", -1},
		{"missing patch counts as zero", "1.2", "1.2.0", 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Compare(tt.a, tt.b); got != tt.want {
				t.Errorf("Compare(%q, %q) = %d, want %d", tt.a, tt.b, got, tt.want)
			}
		})
	}
}
