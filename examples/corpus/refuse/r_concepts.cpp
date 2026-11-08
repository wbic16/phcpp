// refuse: concept-requires
#include <cstdio>
template <typename T>
requires (sizeof(T) >= 2)
T pick(T a, T b) { return a > b ? a : b; }
int main() {
    std::printf("%d\n", pick(2, 3));
    return 0;
}
