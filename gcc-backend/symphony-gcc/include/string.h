#ifndef _STRING_H
#define _STRING_H

void *memcpy(void *destination, const void *source, unsigned int count);
void *memmove(void *destination, const void *source, unsigned int count);
void *memset(void *destination, int value, unsigned int count);
int memcmp(const void *left, const void *right, unsigned int count);

#endif
