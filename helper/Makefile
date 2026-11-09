CC = gcc
PKG = libgpod-1.0
CFLAGS = $(shell pkg-config --cflags $(PKG)) -O2 -Wall
LIBS = $(shell pkg-config --libs $(PKG))
TARGET = ipod_helper

.PHONY: all clean check

all: check $(TARGET)

check:
	@pkg-config --exists $(PKG) || (echo ""; \
	  echo "ERROR: libgpod-1.0 development files not found."; \
	  echo "Install them first, e.g. on Debian/Ubuntu:"; \
	  echo "    sudo apt install libgpod-dev pkg-config build-essential"; \
	  echo ""; exit 1)

$(TARGET): ipod_helper.c
	$(CC) ipod_helper.c -o $(TARGET) $(CFLAGS) $(LIBS)
	@echo "Built ./$(TARGET)"

clean:
	rm -f $(TARGET)
