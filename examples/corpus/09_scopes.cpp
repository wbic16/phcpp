// 09_scopes.cpp — destruction order: nested scopes, early return,
// loops whose bodies own objects across break/continue, an array of
// objects, and range-for by reference and by value.
#include <cstdio>

struct Tracer {
    const char* id;
    Tracer(const char* n) : id(n) {
        std::printf("+%s\n", id);
    }
    ~Tracer() { std::printf("-%s\n", id); }
};

int pick(int x) {
    Tracer t("pick");
    if (x > 0) {
        Tracer u("inner");
        return 1;
    }
    return 0;
}

int main() {
    {
        Tracer a("a");
        {
            Tracer b("b");
        }
        std::printf("mid\n");
    }
    std::printf("pick=%d\n", pick(1));
    for (int i = 0; i < 4; i++) {
        Tracer t("loop");
        if (i == 1) continue;
        if (i == 3) break;
        std::printf("i=%d\n", i);
    }
    Tracer arr[2] = { Tracer("x"), Tracer("y") };
    int nums[3] = { 10, 20, 30 };
    int sum = 0;
    for (int n : nums) sum += n;
    for (const Tracer& t : arr) std::printf("see %s\n", t.id);
    std::printf("sum=%d\n", sum);
    return 0;
}
