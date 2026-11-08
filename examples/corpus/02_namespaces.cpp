// 02_namespaces.cpp — namespaces (nested), namespace-scope functions
// and variables, typedef + using aliases, a class living in a
// namespace, qualified and unqualified lookup.
#include <cstdio>

namespace geo {
namespace inner {
int scale = 3;
int twice(int x) { return x * 2; }
} // namespace inner

typedef int Count;
using Real = double;

struct Meter {
    Real v;
    void set(Real x) { v = x; }
    Real get() const { return v; }
};

Count units() { return inner::scale + 1; }
} // namespace geo

int main() {
    geo::Meter m;
    m.set(2.5);
    geo::Count c = geo::units();
    std::printf("scale=%d units=%d twice=%d v=%.1f\n",
                geo::inner::scale, c, geo::inner::twice(10), m.get());
    return 0;
}
