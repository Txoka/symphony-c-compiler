#ifndef _STDLIB_H
#define _STDLIB_H

void *malloc(unsigned int size);
void *calloc(unsigned int count, unsigned int size);
void *realloc(void *pointer, unsigned int size);
void free(void *pointer);

#endif
