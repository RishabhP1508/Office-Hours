# Build stage: compile a static binary, nothing else ships from here.
FROM golang:1.22 AS build
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY cmd ./cmd
COPY internal ./internal

RUN CGO_ENABLED=0 GOOS=linux go build -o /out/gateway ./cmd/gateway

# Runtime stage: a distroless base with no shell, no package manager, no extra attack surface --
# this process only ever needs to listen on a port and make outbound HTTP/Redis calls.
FROM gcr.io/distroless/static-debian12
COPY --from=build /out/gateway /gateway

EXPOSE 8080
ENTRYPOINT ["/gateway"]
