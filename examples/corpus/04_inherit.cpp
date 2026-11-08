// 04_inherit.cpp — three-level single inheritance: the middle class
// adds a virtual, overrides replace slots in place, one method is
// defined out-of-line, a qualified call suppresses dispatch, and a
// delete through the base pointer runs the full destructor chain.
#include <cstdio>

struct Animal {
    virtual ~Animal() = default;
    virtual const char* sound() const = 0;
    virtual int legs() const { return 4; }
};

struct Dog : Animal {
    const char* sound() const override { return "woof"; }
    virtual int trick() const { return 0; }
    int legs() const override;
};

int Dog::legs() const { return 4; }

struct Puppy : Dog {
    const char* sound() const override { return "yip"; }
    int trick() const override { return 9; }
};

int main() {
    Puppy p;
    Animal* a = &p;
    Dog* d = &p;
    std::printf("%s legs=%d base-legs=%d trick=%d\n",
                a->sound(), a->legs(), a->Animal::legs(), d->trick());
    Animal* heap = new Puppy;
    std::printf("heap %s\n", heap->sound());
    delete heap;
    return 0;
}
