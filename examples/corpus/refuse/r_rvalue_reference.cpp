// refuse: rvalue-reference
#include <cstdio>
void take(int&& x) { std::printf("%d\n", x); }
int main() {
    take(42);
    return 0;
}
