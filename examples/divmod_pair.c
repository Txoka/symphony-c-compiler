/* The paired-divmod pass turns these into one runtime division. */
#include <symphony.h>

int main(void) {
    unsigned int value = input();
    unsigned int remainder = value % 10u;
    unsigned int quotient = value / 10u;
    output(quotient);
    output(remainder);
    return 0;
}
