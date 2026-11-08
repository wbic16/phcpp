// refuse: multiple-inheritance
#include <cstdio>
struct A { int a; };
struct B { int b; };
struct C : A, B { int c; };
int main() {
    C obj;
    std::printf("%d\n", obj.c);
    return 0;
}
