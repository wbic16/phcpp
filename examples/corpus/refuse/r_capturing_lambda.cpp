// refuse: capturing-lambda
#include <cstdio>
int main() {
    int base = 40;
    auto add = [base](int x) -> int { return base + x; };
    std::printf("%d\n", add(2));
    return 0;
}
