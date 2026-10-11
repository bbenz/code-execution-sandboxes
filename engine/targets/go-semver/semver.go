// Package semver compares MAJOR.MINOR.PATCH version strings.
package semver

import "strings"

// Compare returns -1 if a < b, 0 if a == b, and 1 if a > b.
// Missing components count as zero, so "1.2" equals "1.2.0".
func Compare(a, b string) int {
	pa := strings.Split(a, ".")
	pb := strings.Split(b, ".")
	for i := 0; i < 3; i++ {
		if pa[i] < pb[i] {
			return -1
		}
		if pa[i] > pb[i] {
			return 1
		}
	}
	return 0
}
