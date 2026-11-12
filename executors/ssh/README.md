# Venom - Executor SSH

Step to execute a script on a remote server via SSH


## Input

In your yaml file, you can use:

```yaml
  - host mandatory
  - command mandatory
  - user optional (default is OS username)
  - password optional (mandatory if no privatekey is found)
  - privatekey optional (default is $HOME/.ssh/id_rsa)
  - sudo optional
  - sudopassword optional (default to password)
  - knownhosts optional (path of an OpenSSH known_hosts file the host's key must be listed in; exclusive with hostkey)
  - hostkey optional (the one public key the host must present, in authorized_keys format; exclusive with knownhosts)
```

**Host key checking.** By default, neither `knownhosts` nor `hostkey` is set, and the host's key is **not checked**:
any server is accepted. This is the executor's behaviour from before the two keys existed. It suits ephemeral test
hosts whose key changes on every run. Set one of the two keys to check the host:

- `knownhosts`: a file in the format of `~/.ssh/known_hosts`, e.g. the output of `ssh-keyscan -p 2222 192.0.2.10`. A
  relative path is read from the test suite's directory; an absolute path is used as is. The step fails if the host
  is not in the file or presents another key. The file may also trust a certificate authority with a line such as
  `@cert-authority [192.0.2.10]:2222 ssh-ed25519 AAAAC3Nza…`: a host that presents a host certificate signed by that
  authority passes the check, provided the certificate's principals, when it lists any, include the host as written
  in `host` (without the port).
- `hostkey`: one line such as `ssh-ed25519 AAAAC3Nza… [comment]`, e.g. the content of the host's
  `/etc/ssh/ssh_host_ed25519_key.pub`. The step fails if the host presents another key.

The executor asks the host for a key of the type you listed, or for a host certificate when the file lists an
authority for the host. A host with several key types (RSA, ECDSA, Ed25519) therefore passes the check if the file or
`hostkey` holds any one of its keys. Setting both keys, a file that cannot be read or a `hostkey` that is not a public
key fails the step before connecting. So does a `knownhosts` or `hostkey` set to an empty value, such as a variable
that expands to nothing: an empty value is an error, not the same as leaving the key out, so it cannot turn the check
off. Like a connection failure, the reason is in `result.err`.

Example

```yaml

name: Title of TestSuite
testcases:
- name: Check if exit code != 1 and echo command response in less than 1s
  steps:
  - type: ssh
    host: localhost:2222
    command: echo 'foo'
    assertions:
    - result.code ShouldEqual 0
    - result.timeseconds ShouldBeLessThan 1

- name: Use specific privatekey
  steps:
  - type: ssh
    host: 192.0.2.10:2222
    command: echo 'foo'
    user: bar
    privatekey: /home/foo/.ssh/id_rsa
    assertions:
    - result.code ShouldEqual 0

- name: Execute command as another user than bar
  steps:
  - type: ssh
    host: 192.0.2.10:2222
    command: echo 'foo'
    user: bar
    sudo: root
    sudopassword: '{{.mypassword}}'
    assertions:
    - result.code ShouldEqual 0

- name: Check the host's key against a known_hosts file next to the suite
  steps:
  - type: ssh
    host: 192.0.2.10:2222
    command: echo 'foo'
    user: bar
    knownhosts: ssh/known_hosts
    assertions:
    - result.code ShouldEqual 0

- name: Pin the host's key
  steps:
  - type: ssh
    host: 192.0.2.10:2222
    command: echo 'foo'
    user: bar
    hostkey: '{{.hostkey}}'
    assertions:
    - result.code ShouldEqual 0

```
*NB: Sudo option uses a pseudotty*

## Output

```yaml
systemout
systemerr
err
code
timeseconds
```

- result.timeseconds: time of execution
- result.err: if exists, this field contains error
- result.systemout: Standard Output of executed script
- result.systemerr: Error Output of executed script
- result.code: Exit Code

## Default assertion

```yaml
result.code ShouldEqual 0
```
