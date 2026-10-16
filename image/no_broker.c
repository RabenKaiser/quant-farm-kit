/* no_broker.c -- rend la quarantaine Dukascopy RESPECTEE PAR CONSTRUCTION.
 *
 * Compile en /usr/local/lib/no_broker.so et charge par LD_PRELOAD, il fait
 * echouer TOUTE tentative de connexion du terminal : `no connection` garanti,
 * meme si un compte ou une IP etaient tentes. Le testeur fonctionne entierement
 * hors-ligne sur les symboles personnalises (base locale), donc ce stub ne coute
 * rien et supprime toute tentation de reconnexion en masse.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <netdb.h>
#include <sys/socket.h>

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    (void)fd; (void)addr; (void)len;
    errno = ECONNREFUSED;
    return -1;
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res) {
    (void)node; (void)service; (void)hints; (void)res;
    return EAI_NONAME;
}
