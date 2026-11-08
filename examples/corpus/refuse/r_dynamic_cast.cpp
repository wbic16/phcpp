// refuse: rtti-dynamic-cast
#include <cstdio>
struct Base { virtual ~Base() = default; };
struct Derived : Base { int x; };
int main() {
    Derived d;
    Base* b = &d;
    Derived* p = dynamic_cast<Derived*>(b);
    std::printf("%p\n", (void*)p);
    return 0;
}
