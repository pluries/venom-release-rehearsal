This page takes you from nothing to one passing test suite. You need Go and a clone of the venom repository.

## Install

Build the command line tool from a clone of the repository:

```
go build -o venom ./cmd/venom
```

The command writes a `venom` binary in the current folder. When a release exists, you can download its binary instead of building it.

## Write a suite

A suite is a YAML file. This one has one test case with one step. The step runs a shell command, and two assertions check its result:

{{include site/examples/quickstart.yml yaml}}

- `type: exec` runs the `script` in a shell.
- `result.systemout ShouldContainSubstring hello` checks that the output holds the word `hello`.
- `result.code ShouldEqual 0` checks that the command ended without error.

## Run it

Save the file as `quickstart.yml` and run it:

```
./venom run quickstart.yml
```

## Read the result

Venom prints each test case and its status. When every assertion holds, the last line is:

```
final status: PASS
```

A failed assertion is printed with the step, the assertion and the value venom found. The exit code is not zero when a suite fails.

Venom is an independent fork of [ovh/venom](https://github.com/ovh/venom). Next, read the [overview](overview.html) or the page about [test suites](testsuites.html).
