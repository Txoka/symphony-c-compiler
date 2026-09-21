/* Exercises merging of two control-flow paths with one destination. */
#include <symphony.h>

int main(void) {
    if (input())
        goto joined;
    goto joined;

joined:
    return 4;
}
