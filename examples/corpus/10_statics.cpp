// 10_statics.cpp — static data members with out-of-line definitions,
// static member functions (qualified and unqualified calls), default
// arguments, namespace-scope variables, and plain printf alongside
// std::printf.
#include <cstdio>

int g_bias = 5;

struct Counter {
    static int count;
    static int limit;
    int v;
    Counter() : v(0) { count++; }
    static int get() { return count; }
    static void reset() { count = 0; }
};

int Counter::count = 0;
int Counter::limit = 100;

int scale(int x, int mult = 2, int add = 0) { return x * mult + add; }

int main() {
    Counter a;
    Counter b;
    std::printf("count=%d limit=%d via=%d\n",
                Counter::count, Counter::limit, Counter::get());
    Counter::reset();
    std::printf("after reset %d\n", Counter::get());
    printf("scale %d %d %d bias %d\n",
           scale(3), scale(3, 4), scale(3, 4, 5), g_bias);
    return 0;
}
