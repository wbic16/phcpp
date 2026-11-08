// 03_refs.cpp — references (parameters, locals, returns) and
// overloading by arity and by type.
#include <cstdio>

void swap_ints(int& a, int& b) {
    int t = a;
    a = b;
    b = t;
}

int& max_ref(int& a, int& b) { return a > b ? a : b; }

void show(int x) { std::printf("int %d\n", x); }
void show(double x) { std::printf("double %.2f\n", x); }
void show(int x, int y) { std::printf("pair %d %d\n", x, y); }

struct Acc {
    int v;
    void add(const int& x) { v += x; }
    int get() const { return v; }
};

int main() {
    int a = 3, b = 7;
    swap_ints(a, b);
    int& r = max_ref(a, b);
    r = 42;
    const int& cr = a;
    std::printf("a=%d b=%d cr=%d\n", a, b, cr);
    show(1);
    show(2.5);
    show(3, 4);
    Acc acc;
    acc.v = 10;
    acc.add(a);
    std::printf("acc=%d\n", acc.get());
    return 0;
}
