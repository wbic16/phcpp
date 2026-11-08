// slice.cpp — phcpp vertical slice v0 (C++23 source of truth).
//
// Exercises exactly three C++ features, per the slice charter:
//   1. a class with a virtual function (polymorphic dispatch),
//   2. a function template, instantiated twice,
//   3. a destructor with an observable side effect.
// Output is fully deterministic and produced with std::printf so the
// hand-lowered C translation unit can match it byte-for-byte.
#include <cstdio>

struct Shape {
    virtual ~Shape() = default;
    virtual double area() const = 0;
    virtual const char* name() const = 0;
};

struct Circle : Shape {
    double r;
    explicit Circle(double radius) : r(radius) {}
    double area() const override { return 3.141592653589793 * r * r; }
    const char* name() const override { return "circle"; }
};

struct Square : Shape {
    double s;
    explicit Square(double side) : s(side) {}
    double area() const override { return s * s; }
    const char* name() const override { return "square"; }
};

template <typename T>
T add(T a, T b) { return a + b; }

struct Tracer {
    const char* id;
    explicit Tracer(const char* name) : id(name) {
        std::printf("tracer %s constructed\n", id);
    }
    ~Tracer() { std::printf("tracer %s destroyed\n", id); }
};

int main() {
    Circle c(2.0);
    Square q(3.0);
    Shape* shapes[2] = { &c, &q };
    for (Shape* s : shapes)
        std::printf("%s area %.2f\n", s->name(), s->area());
    std::printf("add int %d\n", add(19, 23));
    std::printf("add double %.1f\n", add(1.25, 2.25));
    {
        Tracer t("inner");
        std::printf("inside scope\n");
    }
    std::printf("after scope\n");
    return 0;
}
