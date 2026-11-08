// refuse: operator-overload
#include <cstdio>
struct Vec {
    int x;
    Vec operator+(const Vec& o) const { return Vec{x + o.x}; }
};
int main() {
    Vec a{1}, b{2};
    Vec c = a + b;
    std::printf("%d\n", c.x);
    return 0;
}
