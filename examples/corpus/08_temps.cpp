// 08_temps.cpp — temporaries: return by value, a temporary produced
// and destroyed inside one full expression, a member initialised
// from a function call's by-value result, and copy-initialisation
// (memberwise, the v1 copy rule).
#include <cstdio>

struct Acc {
    int v;
    Acc(int x) : v(x) { std::printf("Acc %d made\n", v); }
    ~Acc() { std::printf("Acc %d gone\n", v); }
    Acc add(const Acc& o) const { return Acc(v + o.v); }
};

Acc make(int x) { return Acc(x); }

struct Wrap {
    Acc inner;
    Wrap() : inner(make(7)) {}
};

int main() {
    Acc a = make(3);
    a.add(Acc(4));
    std::printf("mid %d\n", a.v);
    Acc b = a.add(Acc(1));
    std::printf("b %d\n", b.v);
    Wrap w;
    std::printf("wrap %d\n", w.inner.v);
    Acc c = b;
    std::printf("c %d\n", c.v);
    return 0;
}
