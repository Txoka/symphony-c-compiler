/* Exercises compare -> compare-with-zero -> branch fusion. */
#include <symphony.h>

int main(void) {
    unsigned int left = input();
    unsigned int right = input();

    if ((left < right) != 0)
        return 7;
    return 3;
}
