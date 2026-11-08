// 05_templates.cpp — function templates (one and two type
// parameters), a class template instantiated over a scalar and over
// a class type, and a template whose body calls a method on its
// parameter type (resolved per instantiation).
#include <cstdio>

template <typename T>
T clamp2(T v, T hi) { return v > hi ? hi : v; }

template <typename A, typename B>
struct Pair {
    A first;
    B second;
    Pair(A a, B b) : first(a), second(b) {}
};

template <typename T>
struct Box {
    T v;
    Box(T x) : v(x) {}
    T get() const { return v; }
    void set(T x) { v = x; }
};

struct Token {
    int id;
    int code() const { return id * 10; }
};

template <typename T>
int code_of(T x) { return x.code(); }

int main() {
    std::printf("clamp %d %.1f\n", clamp2(9, 4), clamp2(2.5, 9.5));
    Pair<int, double> pr(3, 4.5);
    std::printf("pair %d %.1f\n", pr.first, pr.second);
    Box<int> bi(41);
    bi.set(bi.get() + 1);
    Box<Token> bt(Token{7});
    std::printf("box %d token %d code_of %d\n",
                bi.get(), bt.get().id, code_of(Token{5}));
    return 0;
}
