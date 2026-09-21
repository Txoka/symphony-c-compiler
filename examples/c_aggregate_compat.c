/* Exercises goto, switch, designated/brace-elided initializers, unions, and copies. */
#include <stdio.h>

struct Pair {
    int left;
    int right;
};

union Word {
    int value;
    unsigned char bytes[4];
};

struct Pair make_pair(int left, int right) {
    struct Pair pair = { left, right };
    return pair;
}

int main(void) {
    struct Pair original = make_pair(10, 20);
    struct Pair selected = { .right = 7, .left = 4 };
    int lookup[5] = { [3] = 9, [1] = 2 };
    union Word selector = { .bytes = { 0, 0, 0, 3 } };

    selected = original;
    goto dispatch;

unexpected:
    return -2;

dispatch:
    switch (selector.value) {
    case 1:
        return 0;
    case 3:
        printf("aggregate compatibility: %d", selected.left + selected.right);
        return selected.left + selected.right + lookup[1] + lookup[3];
    default:
        return -1;
    }
}
