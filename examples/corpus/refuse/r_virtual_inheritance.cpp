// refuse: virtual-inheritance
#include <cstdio>
struct Base { int x; };
struct Mid : virtual Base { int y; };
int main() {
    Mid m;
    std::printf("%d\n", m.y);
    return 0;
}
